import sqlite3
from pathlib import Path
import json
from typing import cast

from trainer.domain import Analysis, CriticalMoment, GameSummary


class SQLiteStorage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                source_id TEXT PRIMARY KEY,
                raw_document TEXT NOT NULL,
                analysis_document TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                opponent TEXT NOT NULL,
                result TEXT NOT NULL,
                speed TEXT NOT NULL,
                critical_count INTEGER NOT NULL
            )
            """
        )
        self._connection.execute("PRAGMA user_version = 2")
        self._connection.commit()

    def save_game(self, raw_document: str, analysis: Analysis) -> None:
        self._connection.execute(
            """
            INSERT INTO games (
                source_id, raw_document, analysis_document, created_at,
                opponent, result, speed, critical_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                raw_document = excluded.raw_document,
                analysis_document = excluded.analysis_document,
                created_at = excluded.created_at,
                opponent = excluded.opponent,
                result = excluded.result,
                speed = excluded.speed,
                critical_count = excluded.critical_count
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
            ),
        )
        self._connection.commit()

    def list_games(self) -> list[GameSummary]:
        rows = self._connection.execute(
            """
            SELECT source_id, opponent, result, speed, critical_count
            FROM games ORDER BY created_at DESC
            """
        ).fetchall()
        return [GameSummary(*cast(tuple[str, str, str, str, int], row)) for row in rows]

    def get_analysis(self, source_id: str) -> Analysis | None:
        row = self._connection.execute(
            "SELECT analysis_document FROM games WHERE source_id = ?", (source_id,)
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
        return Analysis(
            source_id=cast(str, document["source_id"]),
            created_at=cast(int, document["created_at"]),
            opponent=cast(str, document["opponent"]),
            result=cast(str, document["result"]),
            speed=cast(str, document["speed"]),
            time_control=cast(str, document["time_control"]),
            player_color=cast(str, document["player_color"]),
            critical_moments=moments,
        )

    def close(self) -> None:
        self._connection.close()
