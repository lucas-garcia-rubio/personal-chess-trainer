from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import httpx

from trainer.bootstrap import create_app
from trainer.config import Settings


def test_sync_persists_a_lichess_game_and_home_reopens_offline(
    tmp_path: Path,
) -> None:
    fixture = (Path(__file__).parent / "fixtures" / "lichess_game.ndjson").read_text(
        encoding="utf-8"
    )
    requests: list[httpx.Request] = []

    def lichess(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=fixture,
            headers={"content-type": "application/x-ndjson"},
        )

    database_path = tmp_path / "trainer.db"
    settings = Settings(
        lichess_username="Lance5500",
        database_path=database_path,
    )
    app = create_app(settings, lichess_transport=httpx.MockTransport(lichess))

    with TestClient(app) as client:
        response = client.post("/sync", follow_redirects=True)

    assert response.status_code == 200
    assert 'href="/analyses/q7ZvsdUF"' in response.text
    assert "TryingHard87" in response.text
    assert len(requests) == 1
    assert requests[0].url.path == "/api/games/user/Lance5500"
    assert requests[0].url.params["max"] == "1"
    assert requests[0].url.params["analysed"] == "true"
    assert requests[0].url.params["evals"] == "true"
    assert requests[0].url.params["clocks"] == "true"
    assert "authorization" not in requests[0].headers
    assert requests[0].headers["user-agent"].startswith("personal-chess-trainer/")

    with sqlite3.connect(database_path) as database:
        stored_raw = database.execute(
            "SELECT raw_document FROM games WHERE source_id = ?", ("q7ZvsdUF",)
        ).fetchone()
    assert stored_raw == (fixture.strip(),)

    def fail_if_online(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("reopening persisted pages must not call Lichess")

    reopened_app = create_app(
        settings,
        lichess_transport=httpx.MockTransport(fail_if_online),
    )
    with TestClient(reopened_app) as client:
        reopened_home = client.get("/")

    assert reopened_home.status_code == 200
    assert 'href="/analyses/q7ZvsdUF"' in reopened_home.text
