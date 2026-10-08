from collections.abc import Callable, Sequence
import io
import json
from pathlib import Path
import sqlite3

import chess
import chess.pgn
import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from lichess_mock import LichessMock
from trainer.bootstrap import create_app
from trainer.config import EngineSettings, Settings
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
        self.call_count = 0
        self.fail_on_call = fail_on_call

    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun:
        self.call_count += 1
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


class NeverCalledEvaluator:
    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun:
        raise AssertionError("an oversized Import must be rejected before evaluation")


class FailingEvaluator:
    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun:
        raise RuntimeError("the engine failed while evaluating a position")


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
    assert analysis_path == (
        "/analyses/content-sha256/"
        "63015e68308b28ab4fa91b224fb234539bb59e89223eb9830db9f418c5ba82bb"
    )
    assert len(evaluator.positions) == 4
    assert len(set(evaluator.positions)) == 4

    with sqlite3.connect(settings.database_path) as database:
        rows = database.execute(
            "SELECT raw_document, headers_document, analysis_document FROM games"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == PGN.strip()
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


def test_import_rejects_too_many_plies_before_evaluation(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    base_settings = migrated_settings("test-player")
    settings = Settings(
        lichess_username=base_settings.lichess_username,
        database_path=base_settings.database_path,
        engine=EngineSettings(max_plies=2),
    )
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=NeverCalledEvaluator(),
    )

    with TestClient(app) as client:
        assert_rejected(client, settings, PGN, "at most 2 plies", "contains 3")


def test_import_rejects_more_than_one_thousand_plies_by_default(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    game = chess.pgn.Game()
    game.headers.update(
        {"White": "test-player", "Black": "Opponent", "Result": "1/2-1/2"}
    )
    board = game.board()
    node: chess.pgn.GameNode = game
    cycle = ["g1f3", "g8f6", "f3g1", "f6g8"]
    for index in range(1001):
        move = chess.Move.from_uci(cycle[index % len(cycle)])
        node = node.add_variation(move)
        board.push(move)
    pgn = game.accept(
        chess.pgn.StringExporter(headers=True, variations=False, comments=False)
    )
    settings = migrated_settings("test-player")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=NeverCalledEvaluator(),
    )

    with TestClient(app) as client:
        assert_rejected(client, settings, pgn, "at most 1000 plies", "contains 1001")


def test_missing_stockfish_only_fails_the_import_attempt(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    base_settings = migrated_settings("test-player")
    settings = Settings(
        lichess_username=base_settings.lichess_username,
        database_path=base_settings.database_path,
        engine=EngineSettings(
            executable=base_settings.database_path.parent / "missing-stockfish"
        ),
    )
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import and Home must not call Lichess"
        ),
    )

    with TestClient(app) as client:
        home = client.get("/")
        assert_rejected(
            client,
            settings,
            PGN,
            "Could not analyze the Game",
            "Could not start the configured engine executable",
            "[engine] path",
        )

    assert home.status_code == 200


@pytest.mark.parametrize(
    ("scenario", "expected_error", "expects_quit"),
    [
        (
            {"name": "StubEngine 2.3", "positions": {}},
            "exceeded the configured total timeout of 1 second",
            True,
        ),
        (
            {"name": "StubEngine 2.3", "exit_on": "go", "exit_code": 17},
            "exited with code 17 while awaiting",
            False,
        ),
        (
            {
                "name": "StubEngine 2.3",
                "positions": {
                    chess.STARTING_FEN: [
                        "info depth 15 score cp 10 pv zzzz",
                        "bestmove zzzz",
                    ]
                },
            },
            "invalid UCI move &#39;zzzz&#39;",
            True,
        ),
    ],
)
def test_uci_failure_is_reported_without_persistence_and_process_is_stopped(
    scenario: dict[str, object],
    expected_error: str,
    expects_quit: bool,
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "engine"
    executable.write_text(
        (Path(__file__).parent / "uci_engine_stub.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    log_path = tmp_path / "commands.log"
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    monkeypatch.setenv("UCI_ENGINE_LOG", str(log_path))
    monkeypatch.setenv("UCI_ENGINE_SCENARIO", str(scenario_path))
    base_settings = migrated_settings("test-player")
    settings = Settings(
        lichess_username=base_settings.lichess_username,
        database_path=base_settings.database_path,
        engine=EngineSettings(executable=executable, timeout_seconds=1),
    )
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
    )

    with TestClient(app) as client:
        assert_rejected(client, settings, PGN, expected_error)

    commands = log_path.read_text(encoding="utf-8").splitlines()
    if expects_quit:
        assert commands[-1] == "quit"
    else:
        assert commands[-1] == "go depth 15"


def test_import_reports_evaluation_failure_without_persisting(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("test-player")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=FailingEvaluator(),
    )

    with TestClient(app) as client:
        assert_rejected(
            client,
            settings,
            PGN,
            "Could not analyze the Game",
            "engine failed while evaluating a position",
        )


def test_import_ignores_whitespace_around_the_pgn_document(
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
        original = client.post(
            "/imports", data={"pgn": PGN}, follow_redirects=False
        )
        padded = client.post(
            "/imports", data={"pgn": f"\n \t{PGN}\n \t"}, follow_redirects=False
        )

    assert original.status_code == 303
    assert padded.status_code == 303
    assert padded.headers["location"] == original.headers["location"]
    with sqlite3.connect(settings.database_path) as database:
        rows = database.execute(
            "SELECT raw_document FROM games WHERE origin = 'content-sha256'"
        ).fetchall()
    assert rows == [(PGN.strip(),)]


def test_repeated_import_with_cosmetic_pgn_differences_reuses_analysis(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    cosmetic_variant = PGN.replace(
        "1. e4 e5 2. Nf3 1-0",
        "1. e4 {a comment} (1. d4 d5)  e5\n2. Nf3 $1 1-0",
    ).replace('[Event "Local training game"]\n', '[Event "Renamed event"]\n')
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
        original = client.post(
            "/imports", data={"pgn": PGN}, follow_redirects=False
        )
        duplicate = client.post(
            "/imports", data={"pgn": cosmetic_variant}, follow_redirects=False
        )

    assert original.status_code == 303
    assert duplicate.status_code == 303
    assert duplicate.headers["location"] == original.headers["location"]
    assert evaluator.call_count == 1
    with sqlite3.connect(settings.database_path) as database:
        stored = database.execute(
            "SELECT raw_document, analysis_document FROM games"
        ).fetchall()
    assert len(stored) == 1
    assert stored[0][0] == PGN.strip()


def test_content_identity_includes_the_initial_position(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    setup_pgn = PGN.replace(
        '[TimeControl "600+0"]',
        '[TimeControl "600+0"]\n[SetUp "1"]\n'
        '[FEN "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 4 7"]',
    )
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
        standard = client.post(
            "/imports", data={"pgn": PGN}, follow_redirects=False
        )
        setup = client.post(
            "/imports", data={"pgn": setup_pgn}, follow_redirects=False
        )

    assert standard.status_code == 303
    assert setup.status_code == 303
    assert standard.headers["location"] != setup.headers["location"]
    assert evaluator.call_count == 2
    with sqlite3.connect(settings.database_path) as database:
        assert database.execute("SELECT COUNT(*) FROM games").fetchone() == (2,)


def _lichess_pgn(raw_document: str, *, black: str = "TryingHard87") -> str:
    document = json.loads(raw_document)
    game = chess.pgn.Game()
    game.headers.update(
        {
            "Event": "Imported Lichess Game",
            "Site": f'https://lichess.org/{document["id"]}',
            "Date": "2017.12.28",
            "Round": "?",
            "White": document["players"]["white"]["user"]["name"],
            "Black": black,
            "Result": "1/2-1/2",
            "GameId": document["id"],
        }
    )
    board = game.board()
    node: chess.pgn.GameNode = game
    for san in document["moves"].split():
        move = board.parse_san(san)
        node = node.add_variation(move)
        board.push(move)
    return game.accept(
        chess.pgn.StringExporter(headers=True, variations=True, comments=True)
    )


def test_lichess_pgn_reuses_the_analysis_created_by_sync(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    sync_app = create_app(
        settings,
        lichess_transport=lichess_mock.games_for("Lance5500"),
        position_evaluator=DeterministicEvaluator(fail_on_call=True),
    )
    with TestClient(sync_app) as client:
        synced = client.post("/sync", follow_redirects=False)
    assert synced.status_code == 303

    import_app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=DeterministicEvaluator(fail_on_call=True),
    )
    with TestClient(import_app) as client:
        imported = client.post(
            "/imports",
            data={"pgn": _lichess_pgn(lichess_mock.fixture)},
            follow_redirects=False,
        )

    assert imported.status_code == 303
    assert imported.headers["location"] == "/analyses/lichess/q7ZvsdUF"
    with sqlite3.connect(settings.database_path) as database:
        assert database.execute("SELECT COUNT(*) FROM games").fetchone() == (1,)


def test_import_rejects_content_conflicting_with_an_existing_lichess_identity(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    sync_app = create_app(
        settings,
        lichess_transport=lichess_mock.games_for("Lance5500"),
        position_evaluator=DeterministicEvaluator(fail_on_call=True),
    )
    with TestClient(sync_app) as client:
        client.post("/sync")

    conflicting_pgn = _lichess_pgn(lichess_mock.fixture, black="DifferentOpponent")
    import_app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "Import must not call Lichess"
        ),
        position_evaluator=DeterministicEvaluator(fail_on_call=True),
    )
    with TestClient(import_app) as client:
        response = client.post("/imports", data={"pgn": conflicting_pgn})

    assert response.status_code == 422
    assert "conflicts with the existing Lichess Game q7ZvsdUF" in response.text
    with sqlite3.connect(settings.database_path) as database:
        stored = database.execute(
            "SELECT black, raw_document, analysis_document FROM games"
        ).fetchall()
    assert len(stored) == 1
    assert stored[0][0] == "TryingHard87"
    assert stored[0][1] == lichess_mock.fixture.strip()


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
    assert raw_document == pgn.strip()


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
