import json
from pathlib import Path

from fastapi.testclient import TestClient
import httpx

from trainer.bootstrap import create_app
from trainer.config import Settings


def test_analysis_shows_initial_board_and_player_critical_moment(
    tmp_path: Path,
) -> None:
    fixture = (Path(__file__).parent / "fixtures" / "lichess_game.ndjson").read_text(
        encoding="utf-8"
    )

    def lichess(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture)

    settings = Settings(
        lichess_username="Lance5500",
        database_path=tmp_path / "trainer.db",
    )
    app = create_app(settings, lichess_transport=httpx.MockTransport(lichess))
    with TestClient(app) as client:
        client.post("/sync")

    def fail_if_online(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("persisted Analysis must open offline")

    reopened_app = create_app(
        settings,
        lichess_transport=httpx.MockTransport(fail_if_online),
    )
    with TestClient(reopened_app) as client:
        response = client.get("/analyses/q7ZvsdUF")

    assert response.status_code == 200
    assert (
        'data-fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"'
        in response.text
    )
    assert (
        'data-position="r3kb1r/pp2nppp/q7/4N3/3P4/8/PP2QPPP/R1B1K2R w KQ - 4 14"'
        in response.text
    )
    assert 'aria-label="Position before Qf3"' in response.text
    assert "Played: Qf3" in response.text
    assert "Best: Nd3" in response.text
    assert "Mistake" in response.text
    assert "Win%: 66.8 → 54.0" in response.text


def test_analysis_classifies_from_evaluations_not_lichess_judgment(
    tmp_path: Path,
) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "lichess_game.ndjson"
    document = json.loads(fixture_path.read_text(encoding="utf-8"))
    document["id"] = "misleading-judgment"
    document["moves"] = " ".join(document["moves"].split()[:27])
    document["analysis"] = document["analysis"][:27]
    document["analysis"][26]["judgment"]["name"] = "Blunder"

    def lichess(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(document))

    app = create_app(
        Settings(
            lichess_username="Lance5500",
            database_path=tmp_path / "trainer.db",
        ),
        lichess_transport=httpx.MockTransport(lichess),
    )
    with TestClient(app) as client:
        client.post("/sync")
        response = client.get("/analyses/misleading-judgment")

    assert response.status_code == 200
    assert "Mistake" in response.text
    assert "Blunder" not in response.text
