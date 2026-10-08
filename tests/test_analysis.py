from collections.abc import Callable
import json
from pathlib import Path

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from trainer.bootstrap import create_app
from trainer.config import Settings


def _fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


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


def test_analysis_classifies_player_drops_around_winning_chance_thresholds(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    app = create_app(
        migrated_settings("ThresholdPlayer"),
        lichess_transport=lichess_mock.games_for(
            "ThresholdPlayer",
            ndjson=_fixture("lichess_thresholds.ndjson"),
        ),
    )
    with TestClient(app) as client:
        client.post("/sync")
        response = client.get("/analyses/thresholds")

    assert response.status_code == 200
    assert response.text.count('<article class="moment"') == 5
    assert response.text.count("<h3>Inaccuracy</h3>") == 2
    assert response.text.count("<h3>Mistake</h3>") == 2
    assert response.text.count("<h3>Blunder</h3>") == 1
    assert "Played: e5" not in response.text  # 0.0999994, immediately below 0.1
    assert "Played: Nc6" in response.text  # 0.1000048, immediately above 0.1
    assert "Played: a6" in response.text  # 0.1999984, immediately below 0.2
    assert "Played: Nf6" in response.text  # 0.2000003, immediately above 0.2
    assert "Played: Be7" in response.text  # 0.2999996, immediately below 0.3
    assert "Played: b5" in response.text  # 0.3000002, immediately above 0.3
    assert "Played: e4" not in response.text
    assert "Played: Nf3" not in response.text
    assert "Played: Bb5" not in response.text
    assert "Played: Ba4" not in response.text
    assert "Played: O-O" not in response.text
    assert "Played: Re1" not in response.text
    assert "Win%: 65.0 → 60.0" in response.text
    assert "Win%: 54.8 → 44.8" in response.text
    assert "Win%: 13.0 → 3.0" in response.text
    assert "Win%: 78.0 → 63.0" in response.text
    assert "Win%: 44.3 → 29.3" in response.text


def test_analysis_classifies_lichess_mate_transitions(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    app = create_app(
        migrated_settings("MatePlayer"),
        lichess_transport=lichess_mock.games_for(
            "MatePlayer",
            ndjson=_fixture("lichess_mates.ndjson"),
        ),
    )
    with TestClient(app) as client:
        client.post("/sync")
        response = client.get("/analyses/mates")

    assert response.status_code == 200
    assert response.text.count('<article class="moment"') == 7
    assert response.text.count("<h3>Inaccuracy</h3>") == 2
    assert response.text.count("<h3>Mistake</h3>") == 2
    assert response.text.count("<h3>Blunder</h3>") == 3
    assert "Played: Nf3" in response.text  # cp -1000 to being mated
    assert "Played: Bb5" in response.text  # cp -701 to being mated
    assert "Played: Ba4" in response.text  # cp -700 to being mated
    assert "Played: O-O" in response.text  # mate to cp 1000
    assert "Played: Re1" in response.text  # mate to cp 701
    assert "Played: Bb3" in response.text  # mate to cp 700
    assert "Played: c3" in response.text  # winning mate to losing mate
    assert "Win%: 2.5 → 2.5" in response.text
    assert "Win%: 7.0 → 2.5" in response.text
    assert "Win%: 7.1 → 2.5" in response.text
    assert "Win%: 97.5 → 97.5" in response.text
    assert "Win%: 97.5 → 93.0" in response.text
    assert "Win%: 97.5 → 92.9" in response.text
    assert "Win%: 97.5 → 2.5" in response.text
