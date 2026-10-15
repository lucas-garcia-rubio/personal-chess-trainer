from collections.abc import Callable, Sequence
from html import unescape
import json
from pathlib import Path
import re
from typing import cast

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from trainer.bootstrap import create_app
from trainer.config import Settings
from trainer.domain import (
    EvaluationRun,
    EvaluationScore,
    EvaluatorProvenance,
    PositionEvaluation,
)


class DeterministicCandidateEvaluator:
    def __init__(self) -> None:
        self.requests: list[tuple[str, int]] = []

    def evaluate_candidates(self, position: str, max_candidates: int) -> EvaluationRun:
        self.requests.append((position, max_candidates))
        return EvaluationRun(
            provenance=EvaluatorProvenance(
                source_kind="test-double",
                name="Deterministic Evaluator",
                version="1.0",
                parameters={"depth": 12, "multi_pv": max_candidates},
            ),
            evaluations=[
                PositionEvaluation(
                    fen=position,
                    score=EvaluationScore(kind="cp", value=score),
                    best_move=line[0],
                    principal_variation=line,
                )
                for score, line in (
                    (35, ["e2e4", "e7e5", "g1f3"]),
                    (20, ["d2d4", "d7d5"]),
                    (10, ["g1f3", "d7d5"]),
                )
            ],
        )


class PositionOnlyEvaluator:
    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun:
        raise AssertionError("Sync must not use the local Evaluator")


class FailingCandidateEvaluator:
    def evaluate_candidates(self, position: str, max_candidates: int) -> EvaluationRun:
        raise RuntimeError("engine process exited")


def _fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


def _navigation_data(html: str) -> dict[str, object]:
    match = re.search(
        r'<script id="analysis-navigation" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return cast(dict[str, object], json.loads(unescape(match.group(1))))


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
    assert "Chessground(board" in response.text
    assert "coordinates: false" in response.text
    navigation = _navigation_data(response.text)
    positions = navigation["positions"]
    assert isinstance(positions, list)
    assert positions[0]["fen"] == (
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    )
    assert positions[26]["fen"] == (
        "r3kb1r/pp2nppp/q7/4N3/3P4/8/PP2QPPP/R1B1K2R w KQ - 4 14"
    )
    assert 'data-position-index="26"' in response.text
    assert "Played: Qf3" in response.text
    assert "Best: Nd3" in response.text
    assert "Mistake" in response.text
    assert "Win%: 66.8 → 54.0" in response.text


def test_analysis_delivers_one_navigable_board_with_every_game_position(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500"),
    )
    with TestClient(app) as client:
        client.post("/sync")
        response = client.get("/analyses/q7ZvsdUF")

    navigation = _navigation_data(response.text)
    positions = navigation["positions"]
    assert isinstance(positions, list)
    assert response.text.count('class="board"') == 1
    assert positions[0] == {
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "lastMove": None,
    }
    assert positions[1] == {
        "fen": "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1",
        "lastMove": "d2d4",
    }
    assert len(positions) == 126
    assert '<button type="button" data-direction="previous" disabled>' in response.text
    assert '<button type="button" data-direction="next">' in response.text


def test_analysis_previews_the_evaluators_best_line_for_the_selected_game_ply(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    evaluator = DeterministicCandidateEvaluator()
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500"),
        candidate_evaluator=evaluator,
    )
    with TestClient(app) as client:
        client.post("/sync")
        analysis = client.get("/analyses/q7ZvsdUF")
        preview = client.post(
            "/analyses/lichess/q7ZvsdUF/best-line", json={"ply": 0}
        )

    assert analysis.status_code == 200
    assert 'data-action="best-line"' in analysis.text
    assert 'data-url="/analyses/lichess/q7ZvsdUF/best-line"' in analysis.text
    assert "Best Line preview" in analysis.text
    assert preview.status_code == 200
    assert evaluator.requests == [
        (
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            3,
        )
    ]
    document = preview.json()
    assert document.pop("previewId")
    assert document.pop("startingFen") == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    assert document == {
        "label": "Best Line preview",
        "basePly": 0,
        "score": {"kind": "cp", "value": 35},
        "moves": [
            {
                "uci": "e2e4",
                "san": "e4",
                "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            },
            {
                "uci": "e7e5",
                "san": "e5",
                "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            },
            {
                "uci": "g1f3",
                "san": "Nf3",
                "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
            },
        ],
        "provenance": {
            "sourceKind": "test-double",
            "name": "Deterministic Evaluator",
            "version": "1.0",
            "parameters": {"depth": 12, "multi_pv": 3},
        },
    }


def test_analysis_disables_best_line_when_no_candidate_evaluator_is_available(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500"),
        position_evaluator=PositionOnlyEvaluator(),
    )
    with TestClient(app) as client:
        client.post("/sync")
        analysis = client.get("/analyses/q7ZvsdUF")
        preview = client.post(
            "/analyses/lichess/q7ZvsdUF/best-line", json={"ply": 0}
        )

    assert analysis.status_code == 200
    assert 'data-action="best-line" disabled' in analysis.text
    assert "Best Line unavailable: no Evaluator is configured." in analysis.text
    assert preview.status_code == 503
    assert preview.json() == {
        "detail": "Best Line unavailable: no Evaluator is configured."
    }


def test_best_line_score_is_normalized_to_the_black_players_point_of_view(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    evaluator = DeterministicCandidateEvaluator()
    app = create_app(
        migrated_settings("TryingHard87"),
        lichess_transport=lichess_mock.games_for("TryingHard87"),
        candidate_evaluator=evaluator,
    )
    with TestClient(app) as client:
        client.post("/sync")
        preview = client.post(
            "/analyses/lichess/q7ZvsdUF/best-line", json={"ply": 0}
        )

    assert preview.status_code == 200
    assert preview.json()["score"] == {"kind": "cp", "value": -35}


def test_best_line_reports_evaluator_failure_without_a_fallback(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500"),
        candidate_evaluator=FailingCandidateEvaluator(),
    )
    with TestClient(app) as client:
        client.post("/sync")
        preview = client.post(
            "/analyses/lichess/q7ZvsdUF/best-line", json={"ply": 0}
        )

    assert preview.status_code == 503
    assert preview.json() == {
        "detail": "Best Line unavailable: Evaluator unavailable; chess judgment is unverified."
    }


def test_analysis_delivers_a_clickable_evaluation_curve_for_available_scores(
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
            "the evaluation curve must use the persisted Analysis"
        ),
    )
    with TestClient(reopened_app) as client:
        response = client.get("/analyses/q7ZvsdUF")

    navigation = _navigation_data(response.text)
    evaluations = navigation["evaluations"]
    assert isinstance(evaluations, list)
    assert evaluations[:2] == [
        {"positionIndex": 1, "winPercent": 50.7},
        {"positionIndex": 2, "winPercent": 50.6},
    ]
    assert len(evaluations) == 125
    assert '<svg class="evaluation-curve"' in response.text
    assert 'aria-label="Evaluation curve"' in response.text
    assert "showGamePosition(point.positionIndex)" in response.text


def test_analysis_allows_only_legal_client_side_variations_from_known_positions(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500"),
    )
    with TestClient(app) as client:
        client.post("/sync")
        response = client.get("/analyses/q7ZvsdUF")

    assert "chess.js" in response.text
    assert "new Chess(position.fen)" in response.text
    assert "free: false" in response.text
    assert "dests: legalDests(chess)" in response.text
    assert "after: exploreMove" in response.text
    assert "chess.move({ from: orig, to: dest, promotion })" in response.text
    assert "showPosition(currentIndex - 1)" in response.text
    assert "showPosition(currentIndex + 1)" in response.text
    explore_move = response.text.split("function exploreMove", 1)[1].split(
        "function showPosition", 1
    )[0]
    assert "fetch(" not in explore_move


def test_selecting_a_critical_moment_targets_its_pre_move_position_and_highlights(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500"),
    )
    with TestClient(app) as client:
        client.post("/sync")
        response = client.get("/analyses/q7ZvsdUF")

    navigation = _navigation_data(response.text)
    moments = navigation["moments"]
    assert isinstance(moments, dict)
    assert moments["26"] == {"played": "e2f3", "best": "e5d3"}
    assert 'class="moment" data-position-index="26"' in response.text
    assert 'data-brush="red"' in response.text
    assert 'data-brush="green"' in response.text
    assert "ground.setAutoShapes(shapes)" in response.text


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
    assert response.text.count('class="moment-title">Inaccuracy</strong>') == 2
    assert response.text.count('class="moment-title">Mistake</strong>') == 2
    assert response.text.count('class="moment-title">Blunder</strong>') == 1
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
    assert response.text.count('class="moment-title">Inaccuracy</strong>') == 2
    assert response.text.count('class="moment-title">Mistake</strong>') == 2
    assert response.text.count('class="moment-title">Blunder</strong>') == 3
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
