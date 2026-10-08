import sqlite3
from pathlib import Path
import json
from typing import cast

from trainer.application.ports import GameIdentityConflict
from trainer.domain import (
    Analysis,
    CriticalMoment,
    EvaluatorProvenance,
    GameMetadata,
    GameSummary,
    EvaluationScore,
    PositionEvaluation,
    ScoreKind,
)


class SQLiteStorage:
    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")

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

    def list_games(self) -> list[GameSummary]:
        rows = self._connection.execute(
            """
            SELECT origin, origin_id, opponent, result, speed, critical_count
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
            evaluations=evaluations,
        )

    def close(self) -> None:
        self._connection.close()
