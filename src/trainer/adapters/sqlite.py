import sqlite3
from pathlib import Path
import json
from threading import RLock
from typing import Literal, cast

from trainer.application.ports import (
    DiscussionConflict,
    GameIdentityConflict,
    ResponseAttempt,
    TrainerEvent,
)
from trainer.domain import (
    Analysis,
    CriticalMoment,
    Discussion,
    DiscussionMessage,
    EvaluatorProvenance,
    GameMetadata,
    GameSummary,
    EvaluationScore,
    PositionEvaluation,
    ScoreKind,
    VariationMove,
    VariationNode,
)


class SQLiteStorage:
    def __init__(self, path: Path) -> None:
        self._discussion_lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA foreign_keys = ON")

    def response_request(self, request_id: int) -> str:
        with self._discussion_lock:
            row = self._connection.execute(
                "SELECT body FROM discussion_messages WHERE id = ? AND author = 'player'",
                (request_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Trainer request not found")
            return cast(str, row[0])

    def response_attempts(
        self, origin: str, origin_id: str, base_ply: int,
    ) -> list[ResponseAttempt]:
        with self._discussion_lock:
            rows = self._connection.execute(
                """SELECT a.id, a.request_id, a.retry_of, a.revision, a.status
                FROM trainer_attempts a JOIN discussions d ON d.id = a.discussion_id
                WHERE d.game_origin = ? AND d.game_origin_id = ? AND d.base_ply = ?
                ORDER BY a.id""", (origin, origin_id, base_ply),
            ).fetchall()
            return [
                ResponseAttempt(
                    id=row[0], request_id=row[1], retry_of=row[2],
                    revision=row[3], status=row[4],
                    events=[
                        {"sequence": seq, "type": kind, "text": text}
                        for seq, kind, text in self._connection.execute(
                            "SELECT sequence, kind, text FROM trainer_events WHERE attempt_id = ? ORDER BY sequence",
                            (row[0],),
                        )
                    ],
                )
                for row in rows
            ]

    def begin_response(
        self, origin: str, origin_id: str, base_ply: int,
        retry_of: int | None = None,
    ) -> ResponseAttempt:
        with self._discussion_lock, self._connection:
            row = self._connection.execute(
                """SELECT id, revision FROM discussions
                WHERE game_origin = ? AND game_origin_id = ? AND base_ply = ?""",
                (origin, origin_id, base_ply),
            ).fetchone()
            if row is None:
                raise ValueError("Discussion not found")
            discussion_id, revision = row
            self._assert_not_responding(discussion_id)
            if retry_of is None:
                request = self._connection.execute(
                    "SELECT id FROM discussion_messages WHERE discussion_id = ? AND author = 'player' ORDER BY id DESC LIMIT 1",
                    (discussion_id,),
                ).fetchone()
            else:
                request = self._connection.execute(
                    "SELECT request_id FROM trainer_attempts WHERE id = ? AND discussion_id = ? AND status IN ('failed', 'interrupted', 'limited')",
                    (retry_of, discussion_id),
                ).fetchone()
            if request is None:
                raise ValueError("No retryable Trainer request")
            cursor = self._connection.execute(
                "INSERT INTO trainer_attempts (discussion_id, request_id, retry_of, revision, status) VALUES (?, ?, ?, ?, 'running')",
                (discussion_id, request[0], retry_of, revision),
            )
            assert cursor.lastrowid is not None
            attempt_id = cursor.lastrowid
            self._connection.execute(
                "INSERT INTO trainer_events VALUES (?, 1, 'running', '')", (attempt_id,),
            )
        return next(
            a for a in self.response_attempts(origin, origin_id, base_ply)
            if a.id == attempt_id
        )

    def record_response_event(self, attempt_id: int, event: TrainerEvent) -> None:
        with self._discussion_lock, self._connection:
            row = self._connection.execute(
                "SELECT discussion_id, message_id, status FROM trainer_attempts WHERE id = ?", (attempt_id,),
            ).fetchone()
            if row is None or row[2] != "running":
                return
            discussion_id, message_id, _ = row
            if event.type == "text" and event.text:
                if message_id is None:
                    message = self._connection.execute(
                        "INSERT INTO discussion_messages (discussion_id, author, body) VALUES (?, 'trainer', ?)",
                        (discussion_id, event.text),
                    )
                    message_id = message.lastrowid
                    self._connection.execute(
                        "UPDATE trainer_attempts SET message_id = ? WHERE id = ?",
                        (message_id, attempt_id),
                    )
                else:
                    self._connection.execute(
                        "UPDATE discussion_messages SET body = body || ? WHERE id = ?",
                        (event.text, message_id),
                    )
                self._connection.execute(
                    "UPDATE discussions SET revision = revision + 1 WHERE id = ?",
                    (discussion_id,),
                )
            if event.type in {"completed", "interrupted", "failed", "limited"}:
                self._connection.execute(
                    "UPDATE trainer_attempts SET status = ? WHERE id = ?",
                    (event.type, attempt_id),
                )
            self._connection.execute(
                """INSERT INTO trainer_events SELECT ?, COALESCE(MAX(sequence), 0) + 1, ?, ?
                FROM trainer_events WHERE attempt_id = ?""", (attempt_id, event.type, event.text, attempt_id),
            )

    def recover_responses(self) -> None:
        with self._discussion_lock:
            ids = self._connection.execute(
                "SELECT id FROM trainer_attempts WHERE status = 'running'",
            ).fetchall()
            for (attempt_id,) in ids:
                self.record_response_event(attempt_id, TrainerEvent(
                    "interrupted", "Application stopped during the response. Please try again.",
                ))

    def _assert_not_responding(self, discussion_id: int) -> None:
        if self._connection.execute(
            "SELECT id FROM trainer_attempts WHERE discussion_id = ? AND status = 'running'", (discussion_id,),
        ).fetchone() is not None:
            raise DiscussionConflict

    def save_game(
        self, raw_document: str, metadata: GameMetadata, analysis: Analysis
    ) -> None:
        with self._connection:
            inserted = self._connection.execute(
                """
                INSERT INTO games (
                    source_id, raw_document, analysis_document, created_at,
                    opponent, result, speed, critical_count, origin, origin_id,
                    played_at, white, black, game_result, time_control, eco,
                    opening, headers_document, canonical_document
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(origin, origin_id) DO NOTHING
                """,
                (
                    analysis.source_id,
                    raw_document,
                    json.dumps(analysis.to_document()),
                    analysis.created_at,
                    analysis.opponent,
                    analysis.result,
                    analysis.speed,
                    len(analysis.critical_moments),
                    metadata.origin,
                    metadata.origin_id,
                    metadata.played_at,
                    metadata.white,
                    metadata.black,
                    metadata.result,
                    metadata.time_control,
                    metadata.eco,
                    metadata.opening,
                    json.dumps(metadata.headers),
                    metadata.canonical_document,
                ),
            )
            if inserted.rowcount == 1:
                return
            stored = self.get_canonical_document(metadata.origin, metadata.origin_id)
            if stored != metadata.canonical_document:
                raise GameIdentityConflict(metadata.origin, metadata.origin_id)

    def get_canonical_document(self, origin: str, origin_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT canonical_document FROM games WHERE origin = ? AND origin_id = ?",
            (origin, origin_id),
        ).fetchone()
        return None if row is None else cast(str | None, row[0])

    def latest_played_at(self, origin: str) -> int | None:
        row = self._connection.execute(
            "SELECT MAX(played_at) FROM games WHERE origin = ?", (origin,)
        ).fetchone()
        return None if row is None else cast(int | None, row[0])

    def list_games(self) -> list[GameSummary]:
        rows = self._connection.execute(
            """
            SELECT origin, origin_id, opponent, result, time_control, critical_count
            FROM games ORDER BY played_at DESC
            """
        ).fetchall()
        return [
            GameSummary(*cast(tuple[str, str, str, str, str, int], row))
            for row in rows
        ]

    def get_analysis(self, origin: str, origin_id: str) -> Analysis | None:
        row = self._connection.execute(
            "SELECT analysis_document FROM games WHERE origin = ? AND origin_id = ?",
            (origin, origin_id),
        ).fetchone()
        if row is None:
            return None
        document = cast(dict[str, object], json.loads(cast(str, row[0])))
        moment_documents = cast(
            list[dict[str, object]], document.pop("critical_moments")
        )
        moments = [
            CriticalMoment(
                ply=cast(int, moment_document["ply"]),
                position_fen=cast(str, moment_document["position_fen"]),
                played=cast(str, moment_document["played"]),
                best=cast(str, moment_document["best"]),
                classification=cast(str, moment_document["classification"]),
                win_before=cast(float, moment_document["win_before"]),
                win_after=cast(float, moment_document["win_after"]),
            )
            for moment_document in moment_documents
        ]
        evaluation_documents = cast(
            list[dict[str, object]], document.get("evaluations", [])
        )
        evaluations = [
            PositionEvaluation(
                fen=cast(str, evaluation_document["fen"]),
                score=EvaluationScore(
                    kind=cast(
                        ScoreKind,
                        cast(dict[str, object], evaluation_document["score"])[
                            "kind"
                        ],
                    ),
                    value=cast(
                        int,
                        cast(dict[str, object], evaluation_document["score"])[
                            "value"
                        ],
                    ),
                ),
                best_move=cast(str | None, evaluation_document["best_move"]),
                principal_variation=cast(
                    list[str], evaluation_document["principal_variation"]
                ),
            )
            for evaluation_document in evaluation_documents
        ]
        provenance_document = cast(
            dict[str, object],
            document.get(
                "evaluator",
                {
                    "source_kind": "lichess-server",
                    "name": "Lichess",
                    "version": None,
                    "parameters": {},
                },
            ),
        )
        return Analysis(
            source_id=cast(str, document["source_id"]),
            created_at=cast(int, document["created_at"]),
            opponent=cast(str, document["opponent"]),
            result=cast(str, document["result"]),
            speed=cast(str, document["speed"]),
            time_control=cast(str, document["time_control"]),
            player_color=cast(str, document["player_color"]),
            evaluator=EvaluatorProvenance(
                source_kind=cast(str, provenance_document["source_kind"]),
                name=cast(str, provenance_document["name"]),
                version=cast(str | None, provenance_document["version"]),
                parameters=cast(
                    dict[str, int | str | float | bool],
                    provenance_document["parameters"],
                ),
            ),
            critical_moments=moments,
            has_evaluations=cast(bool, document.get("has_evaluations", True)),
            evaluations=evaluations,
        )

    def list_discussions(self, origin: str, origin_id: str) -> list[Discussion]:
        with self._discussion_lock:
            return self._list_discussions(origin, origin_id)

    def _list_discussions(self, origin: str, origin_id: str) -> list[Discussion]:
        rows = self._connection.execute(
            """
            SELECT id, base_ply, base_fen, active_node_id, revision
            FROM discussions
            WHERE game_origin = ? AND game_origin_id = ?
            ORDER BY base_ply
            """,
            (origin, origin_id),
        ).fetchall()
        discussions: list[Discussion] = []
        for discussion_id, base_ply, base_fen, active_node_id, revision in rows:
            message_rows = self._connection.execute(
                """
                SELECT author, body FROM discussion_messages
                WHERE discussion_id = ? ORDER BY id
                """,
                (discussion_id,),
            ).fetchall()
            move_rows = self._connection.execute(
                """
                SELECT id, parent_id, author, move_uci, move_san, resulting_fen
                FROM variation_nodes
                WHERE discussion_id = ? ORDER BY id
                """,
                (discussion_id,),
            ).fetchall()
            nodes = [
                VariationNode(
                    id=cast(int, node_id),
                    parent_id=cast(int | None, parent_id),
                    author=cast(Literal["player", "trainer", "evaluator"], author),
                    uci=cast(str, uci), san=cast(str, san),
                    resulting_fen=cast(str, fen),
                )
                for node_id, parent_id, author, uci, san, fen in move_rows
            ]
            by_id = {node.id: node for node in nodes}
            active_moves: list[VariationMove] = []
            cursor = active_node_id
            while cursor is not None:
                current = by_id[cursor]
                active_moves.append(current)
                cursor = current.parent_id
            active_moves.reverse()
            discussions.append(
                Discussion(
                    base_ply=cast(int, base_ply),
                    base_fen=cast(str, base_fen),
                    messages=[
                        DiscussionMessage(
                            author=cast(Literal["player", "trainer"], author),
                            body=cast(str, body),
                        )
                        for author, body in message_rows
                    ],
                    moves=active_moves,
                    nodes=nodes,
                    active_node_id=cast(int | None, active_node_id),
                    revision=cast(int, revision),
                )
            )
        return discussions

    def create_discussion(
        self, origin: str, origin_id: str, discussion: Discussion
    ) -> None:
        with self._discussion_lock, self._connection:
            inserted = self._connection.execute(
                """
                INSERT INTO discussions (
                    game_origin, game_origin_id, base_ply, base_fen
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(game_origin, game_origin_id, base_ply) DO NOTHING
                """,
                (origin, origin_id, discussion.base_ply, discussion.base_fen),
            )
            if inserted.rowcount == 0:
                return
            discussion_id = inserted.lastrowid
            assert discussion_id is not None
            for message in discussion.messages:
                self._connection.execute(
                    """
                    INSERT INTO discussion_messages (discussion_id, author, body)
                    VALUES (?, ?, ?)
                    """,
                    (discussion_id, message.author, message.body),
                )
            parent_id: int | None = None
            for move in discussion.moves:
                node = self._connection.execute(
                    """
                    INSERT INTO variation_nodes (
                        discussion_id, parent_id, author, move_uci, move_san,
                        resulting_fen
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        discussion_id,
                        parent_id,
                        move.author,
                        move.uci,
                        move.san,
                        move.resulting_fen,
                    ),
                )
                parent_id = node.lastrowid
            if parent_id is not None:
                self._connection.execute(
                    "UPDATE discussions SET active_node_id = ? WHERE id = ?",
                    (parent_id, discussion_id),
                )

    def append_discussion_move(
        self,
        origin: str,
        origin_id: str,
        base_ply: int,
        move: VariationMove,
        revision: int,
        parent_id: int | None,
    ) -> None:
        with self._discussion_lock, self._connection:
            discussion_id = self._advance_revision(
                origin, origin_id, base_ply, revision, trainer=move.author == "trainer",
            )
            self._validate_node(discussion_id, parent_id)
            node = self._connection.execute(
                """
                INSERT INTO variation_nodes (
                    discussion_id, parent_id, author, move_uci, move_san,
                    resulting_fen
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    discussion_id,
                    parent_id,
                    move.author,
                    move.uci,
                    move.san,
                    move.resulting_fen,
                ),
            )
            self._connection.execute(
                "UPDATE discussions SET active_node_id = ? WHERE id = ?",
                (node.lastrowid, discussion_id),
            )

    def append_trainer_line(
        self, origin: str, origin_id: str, base_ply: int, attempt_id: int,
        revision: int, active_node_id: int | None, moves: list[VariationMove],
    ) -> None:
        with self._discussion_lock, self._connection:
            row = self._connection.execute(
                """SELECT d.id FROM discussions d JOIN trainer_attempts a ON a.discussion_id = d.id
                WHERE d.game_origin = ? AND d.game_origin_id = ? AND d.base_ply = ?
                AND d.revision = ? AND d.active_node_id IS ? AND a.id = ? AND a.status = 'running'""",
                (origin, origin_id, base_ply, revision, active_node_id, attempt_id),
            ).fetchone()
            if row is None:
                raise DiscussionConflict
            discussion_id = row[0]
            parent_id = active_node_id
            for move in moves:
                node = self._connection.execute(
                    """INSERT INTO variation_nodes
                    (discussion_id, parent_id, author, move_uci, move_san, resulting_fen)
                    VALUES (?, ?, 'trainer', ?, ?, ?)""",
                    (discussion_id, parent_id, move.uci, move.san, move.resulting_fen),
                )
                parent_id = node.lastrowid
                revision += 1
                payload = json.dumps({"id": parent_id, "parentId": active_node_id,
                                      "author": "trainer", "uci": move.uci, "san": move.san,
                                      "resultingFen": move.resulting_fen, "revision": revision})
                self._connection.execute(
                    """INSERT INTO trainer_events SELECT ?, COALESCE(MAX(sequence), 0) + 1, 'move', ?
                    FROM trainer_events WHERE attempt_id = ?""", (attempt_id, payload, attempt_id),
                )
                active_node_id = parent_id
            self._connection.execute(
                "UPDATE discussions SET active_node_id = ?, revision = ? WHERE id = ?",
                (parent_id, revision, discussion_id),
            )

    def _advance_revision(
        self, origin: str, origin_id: str, base_ply: int, revision: int,
        *, trainer: bool = False,
    ) -> int:
        row = self._connection.execute(
            "SELECT id FROM discussions WHERE game_origin = ? AND game_origin_id = ? AND base_ply = ?",
            (origin, origin_id, base_ply),
        ).fetchone()
        if row is not None and not trainer:
            self._assert_not_responding(row[0])
        updated = self._connection.execute(
            """
            UPDATE discussions SET revision = revision + 1
            WHERE game_origin = ? AND game_origin_id = ? AND base_ply = ?
                AND revision = ?
            RETURNING id
            """,
            (origin, origin_id, base_ply, revision),
        ).fetchone()
        if updated is None:
            raise DiscussionConflict
        return cast(int, updated[0])

    def _validate_node(self, discussion_id: int, node_id: int | None) -> None:
        if node_id is not None and self._connection.execute(
            "SELECT id FROM variation_nodes WHERE id = ? AND discussion_id = ?",
            (node_id, discussion_id),
        ).fetchone() is None:
            raise ValueError("Node does not belong to this Discussion")

    def select_discussion_node(
        self, origin: str, origin_id: str, base_ply: int,
        revision: int, node_id: int | None,
    ) -> None:
        with self._discussion_lock, self._connection:
            discussion_id = self._advance_revision(origin, origin_id, base_ply, revision)
            self._validate_node(discussion_id, node_id)
            self._connection.execute(
                "UPDATE discussions SET active_node_id = ? WHERE id = ?",
                (node_id, discussion_id),
            )

    def close(self) -> None:
        self._connection.close()

    def trainer_consent(self) -> bool:
        row = self._connection.execute(
            "SELECT consent_accepted FROM trainer_preferences WHERE id = 1"
        ).fetchone()
        return row is not None and row[0] == 1

    def get_game_headers(self, origin: str, origin_id: str) -> dict[str, str]:
        row = self._connection.execute(
            "SELECT headers_document FROM games WHERE origin = ? AND origin_id = ?",
            (origin, origin_id),
        ).fetchone()
        if row is None:
            raise ValueError("Game not found")
        return cast(dict[str, str], json.loads(row[0]))

    def get_discussion_session(
        self, origin: str, origin_id: str, base_ply: int
    ) -> str | None:
        row = self._connection.execute(
            """SELECT opencode_session_id FROM discussions
            WHERE game_origin = ? AND game_origin_id = ? AND base_ply = ?""",
            (origin, origin_id, base_ply),
        ).fetchone()
        if row is None:
            raise ValueError("Discussion not found")
        return cast(str | None, row[0])

    def set_discussion_session(
        self, origin: str, origin_id: str, base_ply: int, session_id: str
    ) -> None:
        with self._discussion_lock, self._connection:
            self._connection.execute(
                """UPDATE discussions SET opencode_session_id = ?
                WHERE game_origin = ? AND game_origin_id = ? AND base_ply = ?""",
                (session_id, origin, origin_id, base_ply),
            )

    def append_discussion_message(
        self, origin: str, origin_id: str, base_ply: int, message: DiscussionMessage
    ) -> None:
        with self._discussion_lock, self._connection:
            row = self._connection.execute(
                """UPDATE discussions SET revision = revision + 1
                WHERE game_origin = ? AND game_origin_id = ? AND base_ply = ?
                RETURNING id""",
                (origin, origin_id, base_ply),
            ).fetchone()
            if row is None:
                raise ValueError("Discussion not found")
            self._connection.execute(
                "INSERT INTO discussion_messages (discussion_id, author, body) VALUES (?, ?, ?)",
                (row[0], message.author, message.body),
            )

    def set_trainer_consent(self, accepted: bool) -> None:
        with self._discussion_lock, self._connection:
            self._connection.execute(
                "UPDATE trainer_preferences SET consent_accepted = ? WHERE id = 1",
                (int(accepted),),
            )

    def delete_discussion(
        self, origin: str, origin_id: str, base_ply: int, revision: int,
    ) -> None:
        with self._discussion_lock, self._connection:
            discussion_id = self._advance_revision(origin, origin_id, base_ply, revision)
            self._connection.execute(
                "UPDATE discussions SET active_node_id = NULL WHERE id = ?",
                (discussion_id,),
            )
            self._connection.execute("DELETE FROM discussions WHERE id = ?", (discussion_id,))
