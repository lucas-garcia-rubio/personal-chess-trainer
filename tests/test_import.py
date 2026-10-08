from collections.abc import Callable, Sequence
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from lichess_mock import LichessMock
from trainer.bootstrap import create_app
from trainer.config import Settings
from trainer.domain import (
    EvaluationRun,
    EvaluationScore,
    EvaluatorProvenance,
    PositionEvaluation,
)


PGN = """[Event "Local training game"]
[Site "?"]
[Date "2026.10.05"]
[Round "?"]
[White "TEST-PLAYER"]
[Black "Opponent"]
[Result "1-0"]
[TimeControl "600+0"]
[ECO "C40"]
[Opening "King's Knight Opening"]
[X-Training-Tag "preserve me"]

1. e4 e5 2. Nf3 1-0
"""


class DeterministicEvaluator:
    def __init__(self, *, fail_on_call: bool = False) -> None:
        self.positions: list[str] = []
        self.fail_on_call = fail_on_call

    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun:
        if self.fail_on_call:
            raise AssertionError("persisted Analysis must reopen without evaluation")
        self.positions = list(positions)
        scores = [200, -200, -180, -170]
        best_moves = ["d2d4", "e7e5", "g1f3", None]
        variations = [
            ["d2d4", "d7d5"],
            ["e7e5", "g1f3"],
            ["g1f3", "b8c6"],
            [],
        ]
        return EvaluationRun(
            provenance=EvaluatorProvenance(
                source_kind="test-double",
                name="Deterministic evaluator",
                version="1.0",
                parameters={"depth": 1},
            ),
            evaluations=[
                PositionEvaluation(
                    fen=fen,
                    score=EvaluationScore(kind="cp", value=score),
                    best_move=best_move,
                    principal_variation=variation,
                )
                for fen, score, best_move, variation in zip(
                    positions, scores, best_moves, variations, strict=True
                )
            ],
        )


class BlackPlayerEvaluator:
    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun:
        scores = [0, -300, 300]
        variations = [["e2e4"], ["c7c5", "g1f3"], ["g1f3"]]
        return EvaluationRun(
            provenance=EvaluatorProvenance(
                source_kind="test-double",
                name="Black-side evaluator",
                version=None,
                parameters={},
            ),
            evaluations=[
                PositionEvaluation(
                    fen=fen,
                    score=EvaluationScore(kind="cp", value=score),
                    best_move=variation[0],
                    principal_variation=variation,
                )
                for fen, score, variation in zip(
                    positions, scores, variations, strict=True
                )
            ],
        )


class SetupPositionEvaluator:
    def __init__(self) -> None:
        self.positions: list[str] = []

    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun:
        self.positions = list(positions)
        scores = [-300, 300, 280]
        variations = [
            ["c7c5", "g1f3"],
            ["g1f3"],
            ["b8c6"],
        ]
        return EvaluationRun(
            provenance=EvaluatorProvenance(
                source_kind="test-double",
                name="SetUp position evaluator",
                version=None,
                parameters={},
            ),
            evaluations=[
                PositionEvaluation(
                    fen=fen,
                    score=EvaluationScore(kind="cp", value=score),
                    best_move=variation[0],
                    principal_variation=variation,
                )
                for fen, score, variation in zip(
                    positions, scores, variations, strict=True
                )
            ],
        )


def test_import_pgn_redirects_to_persisted_analysis_and_reopens_offline(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("test-player")
    evaluator = DeterministicEvaluator()
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=evaluator,
    )

    with TestClient(app) as client:
        home = client.get("/")
        form = client.get("/imports/new")
        imported = client.post("/imports", data={"pgn": PGN}, follow_redirects=False)

    assert home.status_code == 200
    assert 'href="/imports/new"' in home.text
    assert form.status_code == 200
    assert '<textarea name="pgn"' in form.text
    assert imported.status_code == 303
    analysis_path = imported.headers["location"]
    assert analysis_path.startswith("/analyses/content-sha256/")
    assert len(evaluator.positions) == 4
    assert len(set(evaluator.positions)) == 4

    with sqlite3.connect(settings.database_path) as database:
        rows = database.execute(
            "SELECT raw_document, headers_document, analysis_document FROM games"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == PGN
    assert json.loads(rows[0][1])["X-Training-Tag"] == "preserve me"
    analysis_document = json.loads(rows[0][2])
    assert analysis_document["evaluator"] == {
        "source_kind": "test-double",
        "name": "Deterministic evaluator",
        "version": "1.0",
        "parameters": {"depth": 1},
    }
    assert len(analysis_document["evaluations"]) == 4
    assert analysis_document["evaluations"][0]["principal_variation"] == [
        "d2d4",
        "d7d5",
    ]

    reopened_app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "persisted Analysis must reopen without Lichess"
        ),
        position_evaluator=DeterministicEvaluator(fail_on_call=True),
    )
    with TestClient(reopened_app) as client:
        analysis = client.get(analysis_path)

    assert analysis.status_code == 200
    assert "Analysis vs Opponent" in analysis.text
    assert "Played: e4" in analysis.text
    assert "Best: d4" in analysis.text
    assert "Blunder" in analysis.text


def assert_rejected(
    client: TestClient,
    settings: Settings,
    pgn: str,
    *fragments: str,
) -> None:
    response = client.post("/imports", data={"pgn": pgn})
    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert str(escape(pgn)) in response.text
    for fragment in fragments:
        assert fragment in response.text
    with sqlite3.connect(settings.database_path) as database:
        persisted = database.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    assert persisted == 0


def test_import_rejects_a_second_game(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    second_game = PGN.replace("TEST-PLAYER", "another-player").replace(
        "Local training game", "A second Game"
    )
    settings = migrated_settings("test-player")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=DeterministicEvaluator(),
    )

    with TestClient(app) as client:
        assert_rejected(
            client, settings, PGN + "\n" + second_game, "exactly one Game"
        )


def test_import_rejects_leftover_game_content_after_the_first_game(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("test-player")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=DeterministicEvaluator(),
    )

    with TestClient(app) as client:
        assert_rejected(
            client, settings, PGN + "\n1. d4 d5 2. c4", "exactly one Game"
        )


def test_import_requires_white_black_and_result_headers(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("test-player")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=DeterministicEvaluator(),
    )

    with TestClient(app) as client:
        assert_rejected(
            client,
            settings,
            PGN.replace('[White "TEST-PLAYER"]\n', ""),
            "must include the White",
        )
        assert_rejected(
            client,
            settings,
            PGN.replace('[Black "Opponent"]\n', ""),
            "must include the Black",
        )
        assert_rejected(
            client,
            settings,
            PGN.replace('[Result "1-0"]\n', ""),
            "must include the Result",
        )
        assert_rejected(
            client,
            settings,
            PGN.replace('[Result "1-0"]', '[Result "*"]'),
            "Result header must be",
        )


def test_import_rejects_a_result_disagreement_between_header_and_movetext(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("test-player")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=DeterministicEvaluator(),
    )

    with TestClient(app) as client:
        assert_rejected(
            client,
            settings,
            PGN.replace("1. e4 e5 2. Nf3 1-0", "1. e4 e5 2. Nf3 0-1"),
            "does not match the movetext result",
        )
        assert_rejected(
            client,
            settings,
            PGN.replace("1. e4 e5 2. Nf3 1-0", "1. e4 e5 2. Nf3"),
            "does not match the movetext result",
        )


def test_import_rejects_a_game_not_played_by_the_configured_player(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("somebody-else")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=DeterministicEvaluator(),
    )

    with TestClient(app) as client:
        assert_rejected(
            client,
            settings,
            PGN,
            "configured Player must be White or Black",
        )


def test_import_rejects_illegal_movetext(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("test-player")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=DeterministicEvaluator(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/imports", data={"pgn": PGN.replace("2. Nf3", "2. Ke3")}
        )

    assert response.status_code == 422
    assert "Could not parse the PGN" in response.text
    assert "illegal san" in response.text
    assert "while parsing" not in response.text
    with sqlite3.connect(settings.database_path) as database:
        persisted = database.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    assert persisted == 0


@pytest.mark.parametrize("variant", ["Chess960", "Crazyhouse", "Bughouse"])
def test_import_rejects_unsupported_variants_explicitly(
    variant: str,
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("test-player")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=DeterministicEvaluator(),
    )
    pgn = PGN.replace(
        '[TimeControl "600+0"]', f'[TimeControl "600+0"]\n[Variant "{variant}"]'
    )

    with TestClient(app) as client:
        assert_rejected(
            client, settings, pgn, "Standard Games only", variant
        )


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ('[SetUp "1"]', "requires a FEN header"),
        (
            '[SetUp "0"]\n[FEN "8/8/8/8/8/4k3/8/4K2R w K - 0 1"]',
            "requires SetUp",
        ),
        ('[SetUp "1"]\n[FEN "not-a-position"]', "Could not parse the FEN"),
    ],
)
def test_import_rejects_incompatible_setup_headers(
    headers: str,
    message: str,
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("test-player")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=DeterministicEvaluator(),
    )
    pgn = PGN.replace('[TimeControl "600+0"]', f'[TimeControl "600+0"]\n{headers}')

    with TestClient(app) as client:
        assert_rejected(client, settings, pgn, message)


def test_import_honors_a_setup_position_and_only_analyzes_the_main_line(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    pgn = """[Event "SetUp training game"]
[White "Opponent"]
[Black "PLAYER"]
[Result "0-1"]
[SetUp "1"]
[FEN "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"]
[X-Training-Tag "preserve me exactly"]

1... e5 $1 {main-line comment} (1... c5 {side variation}) 2. Nf3 0-1
"""
    settings = migrated_settings("player")
    evaluator = SetupPositionEvaluator()
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=evaluator,
    )

    with TestClient(app) as client:
        response = client.post("/imports", data={"pgn": pgn})

    assert response.status_code == 200
    assert evaluator.positions == [
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
    ]
    assert "Played: e5" in response.text
    assert "Best: c5" in response.text
    assert 'data-orientation="black"' in response.text
    assert f'data-position="{evaluator.positions[0]}"' in response.text
    with sqlite3.connect(settings.database_path) as database:
        raw_document = database.execute(
            "SELECT raw_document FROM games"
        ).fetchone()[0]
    assert raw_document == pgn


def test_import_analyzes_from_the_black_players_point_of_view(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    pgn = """[White "Opponent"]
[Black "PLAYER"]
[Result "0-1"]

1. e4 e5 0-1
"""
    app = create_app(
        migrated_settings("player"),
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=BlackPlayerEvaluator(),
    )

    with TestClient(app) as client:
        response = client.post("/imports", data={"pgn": pgn})

    assert response.status_code == 200
    assert "Analysis vs Opponent" in response.text
    assert "Played: e5" in response.text
    assert "Best: c5" in response.text
    assert "Blunder" in response.text
