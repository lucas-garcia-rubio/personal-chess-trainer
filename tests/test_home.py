from collections.abc import Callable
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from trainer.bootstrap import create_app
from trainer.config import Settings


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
