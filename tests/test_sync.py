from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from lichess_mock import LichessMock
from trainer.bootstrap import create_app
from trainer.config import Settings
from trainer.domain import EvaluationRun


class UnusedLocalEvaluator:
    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun:
        raise AssertionError("Sync must not evaluate locally; it uses the server's evaluations")


def test_sync_persists_a_lichess_game_and_home_reopens_offline(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.games_for("Lance5500"),
        position_evaluator=UnusedLocalEvaluator(),
    )

    with TestClient(app) as client:
        response = client.post("/sync", follow_redirects=True)

    assert response.status_code == 200
    assert 'href="/analyses/lichess/q7ZvsdUF"' in response.text
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

    with sqlite3.connect(settings.database_path) as database:
        stored = database.execute(
            """
            SELECT raw_document, origin, origin_id, played_at, white, black,
                   game_result, time_control, eco, opening, headers_document,
                   analysis_document
            FROM games WHERE source_id = ?
            """,
            ("q7ZvsdUF",),
        ).fetchone()
    assert stored is not None
    assert stored[:10] == (
        lichess_mock.fixture.strip(),
        "lichess",
        "q7ZvsdUF",
        1514505150000,
        "Lance5500",
        "TryingHard87",
        "1/2-1/2",
        "300+3",
        "D31",
        "Semi-Slav Defense: Marshall Gambit",
    )
    headers = json.loads(stored[10])
    assert headers == {
        "Event": "2017 Winter Marathon",
        "Site": "https://lichess.org/q7ZvsdUF",
        "Date": "2017.12.28",
        "White": "Lance5500",
        "Black": "TryingHard87",
        "Result": "1/2-1/2",
        "GameId": "q7ZvsdUF",
        "UTCDate": "2017.12.28",
        "UTCTime": "23:52:30",
        "WhiteElo": "2389",
        "BlackElo": "2498",
        "WhiteRatingDiff": "+4",
        "BlackRatingDiff": "-4",
        "Variant": "standard",
        "TimeControl": "300+3",
        "ECO": "D31",
        "Opening": "Semi-Slav Defense: Marshall Gambit",
        "Termination": "draw",
    }
    analysis = json.loads(stored[11])
    assert analysis["evaluator"] == {
        "source_kind": "lichess-server",
        "name": "Lichess",
        "version": None,
        "parameters": {},
    }

    reopened_app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "reopening persisted pages must not call Lichess"
        ),
    )
    with TestClient(reopened_app) as client:
        reopened_home = client.get("/")

    assert reopened_home.status_code == 200
    assert 'href="/analyses/lichess/q7ZvsdUF"' in reopened_home.text


def test_sync_falls_back_safely_when_a_game_reports_no_valid_date(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    undated = json.loads(lichess_mock.fixture)
    del undated["createdAt"]
    started = datetime.now(tz=timezone.utc)
    app = create_app(
        settings,
        lichess_transport=lichess_mock.games_for(
            "Lance5500", ndjson=json.dumps(undated)
        ),
        position_evaluator=UnusedLocalEvaluator(),
    )

    with TestClient(app) as client:
        response = client.post("/sync", follow_redirects=True)

    finished = datetime.now(tz=timezone.utc)
    assert response.status_code == 200
    assert 'href="/analyses/lichess/q7ZvsdUF"' in response.text
    with sqlite3.connect(settings.database_path) as database:
        stored = database.execute(
            "SELECT played_at, headers_document FROM games WHERE origin_id = ?",
            ("q7ZvsdUF",),
        ).fetchone()
    assert stored is not None
    played_at, headers_document = stored
    assert (
        int(started.timestamp() * 1000)
        <= played_at
        <= int(finished.timestamp() * 1000)
    )
    headers = json.loads(headers_document)
    assert all(
        key not in headers for key in ("Date", "UTCDate", "UTCTime")
    )


def test_sync_persists_no_invented_metadata_for_absent_values(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    minimal = json.loads(lichess_mock.fixture)
    for player in minimal["players"].values():
        del player["rating"]
        del player["ratingDiff"]
    del minimal["opening"]
    del minimal["arenaTour"]
    del minimal["source"]
    app = create_app(
        settings,
        lichess_transport=lichess_mock.games_for(
            "Lance5500", ndjson=json.dumps(minimal)
        ),
        position_evaluator=UnusedLocalEvaluator(),
    )

    with TestClient(app) as client:
        response = client.post("/sync", follow_redirects=True)

    assert response.status_code == 200
    with sqlite3.connect(settings.database_path) as database:
        stored = database.execute(
            "SELECT eco, opening, headers_document FROM games WHERE origin_id = ?",
            ("q7ZvsdUF",),
        ).fetchone()
    assert stored is not None
    eco, opening, headers_document = stored
    assert eco is None
    assert opening is None
    assert json.loads(headers_document) == {
        "Site": "https://lichess.org/q7ZvsdUF",
        "Date": "2017.12.28",
        "White": "Lance5500",
        "Black": "TryingHard87",
        "Result": "1/2-1/2",
        "GameId": "q7ZvsdUF",
        "UTCDate": "2017.12.28",
        "UTCTime": "23:52:30",
        "Variant": "standard",
        "TimeControl": "300+3",
        "Termination": "draw",
    }


def test_sync_fails_locally_for_an_unexpected_lichess_request(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    app = create_app(
        migrated_settings("unexpected-player"),
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
