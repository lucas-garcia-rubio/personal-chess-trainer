from collections.abc import Callable
import json

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from trainer.bootstrap import create_app
from trainer.config import Settings


def test_analysis_shows_initial_board_and_player_critical_moment(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.games_for("Lance5500"),
    )
    with TestClient(app) as client:
        client.post("/sync")

    reopened_app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "persisted Analysis must open offline"
        ),
    )
    with TestClient(reopened_app) as client:
        response = client.get("/analyses/q7ZvsdUF")

    assert response.status_code == 200
    assert "@lichess-org/chessground@10.4.2/assets/chessground.base.css" in response.text
    assert "@lichess-org/chessground@10.4.2/assets/chessground.brown.css" in response.text
    assert "@lichess-org/chessground@10.4.2/assets/chessground.cburnett.css" in response.text
    assert "@lichess-org/chessground@10.4.2/+esm" in response.text
    assert "Chessground(element" in response.text
    assert "coordinates: false" in response.text
    assert "<span>" not in response.text
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
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    document = json.loads(lichess_mock.fixture)
    document["id"] = "misleading-judgment"
    document["moves"] = " ".join(document["moves"].split()[:27])
    document["analysis"] = document["analysis"][:27]
    document["analysis"][26]["judgment"]["name"] = "Blunder"

    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for(
            "Lance5500",
            ndjson=json.dumps(document),
        ),
    )
    with TestClient(app) as client:
        client.post("/sync")
        response = client.get("/analyses/misleading-judgment")

    assert response.status_code == 200
    assert "Mistake" in response.text
    assert "Blunder" not in response.text
