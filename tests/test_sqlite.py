from collections.abc import Callable
from dataclasses import replace
import json
import sqlite3
from threading import Timer
import time

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from trainer.adapters.sqlite import SQLiteStorage
from trainer.bootstrap import create_app
from trainer.config import Settings
from trainer.domain import Analysis, EvaluatorProvenance, GameMetadata


def _analysis(source_id: str) -> Analysis:
    return Analysis(
        source_id=source_id,
        created_at=1_700_000_000_000,
        opponent="Opponent",
        result="win",
        speed="rapid",
        time_control="600+0",
        player_color="white",
        evaluator=EvaluatorProvenance(
            source_kind="test-double",
            name="deterministic",
            version="1",
            parameters={"depth": 1},
        ),
        critical_moments=[],
    )


def _metadata(origin: str, origin_id: str) -> GameMetadata:
    return GameMetadata(
        origin=origin,
        origin_id=origin_id,
        played_at=1_700_000_000_000,
        white="Player",
        black="Opponent",
        result="1-0",
        time_control="600+0",
        eco=None,
        opening=None,
        headers={
            "White": "Player",
            "Black": "Opponent",
            "Result": "1-0",
            "X-Provider-Tag": "preserved verbatim",
        },
    )


def test_origin_identity_and_unknown_headers_are_preserved(
    migrated_settings: Callable[[str], Settings],
) -> None:
    settings = migrated_settings("Player")
    storage = SQLiteStorage(settings.database_path)
    try:
        analysis = _analysis("shared-id")
        storage.save_game("raw-one", _metadata("lichess", "shared-id"), analysis)
        storage.save_game(
            "raw-two",
            _metadata("content-sha256", "shared-id"),
            replace(analysis, opponent="Other opponent"),
        )
        lichess_analysis = storage.get_analysis("lichess", "shared-id")
        content_analysis = storage.get_analysis("content-sha256", "shared-id")
    finally:
        storage.close()

    assert lichess_analysis is not None
    assert lichess_analysis.opponent == "Opponent"
    assert content_analysis is not None
    assert content_analysis.opponent == "Other opponent"
    with sqlite3.connect(settings.database_path) as database:
        rows = database.execute(
            "SELECT origin, raw_document, headers_document FROM games "
            "ORDER BY origin"
        ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        ("content-sha256", "raw-two"),
        ("lichess", "raw-one"),
    ]
    assert all(
        json.loads(row[2])["X-Provider-Tag"] == "preserved verbatim"
        for row in rows
    )


def test_sqlite_allows_reads_during_a_write_and_waits_for_brief_contention(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.games_for("Lance5500"),
    )
    writer = sqlite3.connect(settings.database_path, check_same_thread=False)

    with TestClient(app) as client:
        writer.execute("BEGIN IMMEDIATE")
        home = client.get("/")
        assert home.status_code == 200

        release = Timer(0.1, writer.commit)
        release.start()
        started = time.monotonic()
        synced = client.post("/sync", follow_redirects=False)
        elapsed = time.monotonic() - started
        release.join(timeout=1)

    writer.close()
    assert synced.status_code == 303
    assert 0.05 <= elapsed < 2
