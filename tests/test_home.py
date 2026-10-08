from collections.abc import Callable
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from trainer.adapters.sqlite import SQLiteStorage
from trainer.bootstrap import create_app
from trainer.config import Settings
from trainer.domain import Analysis, EvaluatorProvenance, GameMetadata


def test_home_is_served_with_a_real_local_database(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("test-player")

    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "rendering Home must not call Lichess"
        ),
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Personal Chess Trainer" in response.text
    assert settings.database_path.is_file()
    with sqlite3.connect(settings.database_path) as database:
        assert database.execute("PRAGMA user_version").fetchone() == (3,)


def test_home_orders_games_by_their_operational_instant(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    older = json.loads(lichess_mock.fixture)
    older["id"] = "older"
    older["createdAt"] = 1_600_000_000_000
    older["players"]["black"]["user"]["name"] = "Older Opponent"
    newer = json.loads(lichess_mock.fixture)
    newer["id"] = "newer"
    newer["createdAt"] = 1_700_000_000_000
    newer["players"]["black"]["user"]["name"] = "Newer Opponent"
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for(
            "Lance5500", ndjson=f"{json.dumps(older)}\n{json.dumps(newer)}"
        ),
    )

    with TestClient(app) as client:
        response = client.post("/sync", follow_redirects=True)

    assert response.text.index("Newer Opponent") < response.text.index("Older Opponent")


def _stored_game(
    *, origin_id: str, opponent: str, played_at: int, created_at: int
) -> tuple[GameMetadata, Analysis]:
    return (
        GameMetadata(
            origin="lichess",
            origin_id=origin_id,
            played_at=played_at,
            white="Player",
            black=opponent,
            result="1-0",
            time_control="600+0",
            eco=None,
            opening=None,
            headers={"Result": "1-0"},
        ),
        Analysis(
            source_id=origin_id,
            created_at=created_at,
            opponent=opponent,
            result="win",
            speed="rapid",
            time_control="600+0",
            player_color="white",
            evaluator=EvaluatorProvenance(
                source_kind="test-double",
                name="deterministic",
                version="1",
                parameters={},
            ),
            critical_moments=[],
        ),
    )


def test_home_orders_by_the_operational_date_not_the_analysis_instant(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("test-player")
    later_metadata, later_analysis = _stored_game(
        origin_id="later",
        opponent="Later Played Opponent",
        played_at=2_000_000_000_000,
        created_at=1_000_000_000_000,
    )
    earlier_metadata, earlier_analysis = _stored_game(
        origin_id="earlier",
        opponent="Earlier Played Opponent",
        played_at=1_000_000_000_000,
        created_at=2_000_000_000_000,
    )
    storage = SQLiteStorage(settings.database_path)
    try:
        storage.save_game("raw-later", later_metadata, later_analysis)
        storage.save_game("raw-earlier", earlier_metadata, earlier_analysis)
    finally:
        storage.close()

    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "rendering Home must not call Lichess"
        ),
    )
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert (
        response.text.index("Later Played Opponent")
        < response.text.index("Earlier Played Opponent")
    )
