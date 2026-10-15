import sqlite3
from pathlib import Path
import json
from typing import Literal, cast

from trainer.application.ports import GameIdentityConflict
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
)


class SQLiteStorage:
    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA foreign_keys = ON")

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
        rows = self._connection.execute(
            """
            SELECT id, base_ply, base_fen
            FROM discussions
            WHERE game_origin = ? AND game_origin_id = ?
            ORDER BY base_ply
            """,
            (origin, origin_id),
        ).fetchall()
        discussions: list[Discussion] = []
        for discussion_id, base_ply, base_fen in rows:
            message_rows = self._connection.execute(
                """
                SELECT author, body FROM discussion_messages
                WHERE discussion_id = ? ORDER BY id
                """,
                (discussion_id,),
            ).fetchall()
            move_rows = self._connection.execute(
                """
                SELECT author, move_uci, move_san, resulting_fen
                FROM variation_nodes
                WHERE discussion_id = ? ORDER BY id
                """,
                (discussion_id,),
            ).fetchall()
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
                    moves=[
                        VariationMove(
                            author=cast(
                                Literal["player", "trainer", "evaluator"], author
                            ),
                            uci=cast(str, uci),
                            san=cast(str, san),
                            resulting_fen=cast(str, resulting_fen),
                        )
                        for author, uci, san, resulting_fen in move_rows
                    ],
                )
            )
        return discussions

    def create_discussion(
        self, origin: str, origin_id: str, discussion: Discussion
    ) -> None:
        with self._connection:
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
    ) -> None:
        with self._connection:
            row = self._connection.execute(
                """
                SELECT id, active_node_id
                FROM discussions
                WHERE game_origin = ? AND game_origin_id = ? AND base_ply = ?
                """,
                (origin, origin_id, base_ply),
            ).fetchone()
            if row is None:
                raise ValueError("Discussion not found")
            discussion_id, parent_id = row
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

    def get_discussion_session(self, origin: str, origin_id: str, base_ply: int) -> str | None:
        row = self._connection.execute(
            "SELECT opencode_session_id FROM discussions WHERE game_origin = ? AND game_origin_id = ? AND base_ply = ?",
            (origin, origin_id, base_ply),
        ).fetchone()
        if row is None:
            raise ValueError("Discussion not found")
        return cast(str | None, row[0])

    def set_discussion_session(self, origin: str, origin_id: str, base_ply: int, session_id: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE discussions SET opencode_session_id = ? WHERE game_origin = ? AND game_origin_id = ? AND base_ply = ?",
                (session_id, origin, origin_id, base_ply),
            )

    def append_discussion_message(self, origin: str, origin_id: str, base_ply: int, message: DiscussionMessage) -> None:
        with self._connection:
            self._connection.execute(
                """INSERT INTO discussion_messages (discussion_id, author, body)
                SELECT id, ?, ? FROM discussions
                WHERE game_origin = ? AND game_origin_id = ? AND base_ply = ?""",
                (message.author, message.body, origin, origin_id, base_ply),
            )

    def discussion_positions(self, origin: str, origin_id: str, base_ply: int) -> dict[str, object]:
        row = self._connection.execute(
            """SELECT d.id, d.active_node_id, COALESCE(n.resulting_fen, d.base_fen)
            FROM discussions d LEFT JOIN variation_nodes n ON n.id = d.active_node_id
            WHERE d.game_origin = ? AND d.game_origin_id = ? AND d.base_ply = ?""",
            (origin, origin_id, base_ply),
        ).fetchone()
        if row is None:
            raise ValueError("Discussion not found")
        nodes = self._connection.execute(
            "SELECT id, parent_id, author, move_uci, move_san, resulting_fen FROM variation_nodes WHERE discussion_id = ? ORDER BY id",
            (row[0],),
        ).fetchall()
        return {
            "active_node_id": row[1], "active_position": row[2],
            "variations": [
                dict(zip(("id", "parent_id", "author", "uci", "san", "fen"), node))
                for node in nodes
            ],
        }

    def set_trainer_consent(self, accepted: bool) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE trainer_preferences SET consent_accepted = ? WHERE id = 1",
                (int(accepted),),
            )
