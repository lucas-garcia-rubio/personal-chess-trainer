from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from lichess_mock import LichessMock
from trainer.bootstrap import create_app
from trainer.config import Settings


def test_sync_persists_a_lichess_game_and_home_reopens_offline(
    tmp_path: Path,
    lichess_mock: LichessMock,
) -> None:
    database_path = tmp_path / "trainer.db"
    settings = Settings(
        lichess_username="Lance5500",
        database_path=database_path,
    )
    app = create_app(
        settings,
        lichess_transport=lichess_mock.games_for("Lance5500"),
    )

    with TestClient(app) as client:
        response = client.post("/sync", follow_redirects=True)

    assert response.status_code == 200
    assert 'href="/analyses/q7ZvsdUF"' in response.text
    assert "TryingHard87" in response.text
    assert len(lichess_mock.requests) == 1
    request = lichess_mock.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/api/games/user/Lance5500"
    assert request.url.params["max"] == "1"
    assert request.url.params["analysed"] == "true"
    assert request.url.params["evals"] == "true"
    assert request.url.params["clocks"] == "true"
    assert request.headers["accept"] == "application/x-ndjson"
    assert "authorization" not in request.headers
    assert request.headers["user-agent"].startswith("personal-chess-trainer/")

    with sqlite3.connect(database_path) as database:
        stored_raw = database.execute(
            "SELECT raw_document FROM games WHERE source_id = ?", ("q7ZvsdUF",)
        ).fetchone()
    assert stored_raw == (lichess_mock.fixture.strip(),)

    reopened_app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "reopening persisted pages must not call Lichess"
        ),
    )
    with TestClient(reopened_app) as client:
        reopened_home = client.get("/")

    assert reopened_home.status_code == 200
    assert 'href="/analyses/q7ZvsdUF"' in reopened_home.text


def test_sync_fails_locally_for_an_unexpected_lichess_request(
    tmp_path: Path,
    lichess_mock: LichessMock,
) -> None:
    app = create_app(
        Settings(
            lichess_username="unexpected-player",
            database_path=tmp_path / "trainer.db",
        ),
        lichess_transport=lichess_mock.games_for("expected-player"),
    )

    with TestClient(app) as client:
        with pytest.raises(
            AssertionError,
            match=(
                "Unexpected Lichess request path: "
                "/api/games/user/unexpected-player; "
                "expected /api/games/user/expected-player"
            ),
        ):
            client.post("/sync")
