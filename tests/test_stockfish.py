from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil

import pytest

from trainer.adapters.stockfish import StockfishError, StockfishEvaluator
from trainer.config import EngineSettings
from trainer.domain import EvaluationScore, EvaluatorProvenance, PositionEvaluation

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
FOOLS_MATE_FEN = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"


@dataclass
class StubEngine:
    executable: Path
    log_path: Path
    scenario_path: Path

    def script(self, scenario: dict[str, object]) -> None:
        self.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    def commands(self) -> list[str]:
        return self.log_path.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def stub_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StubEngine:
    source = Path(__file__).parent / "uci_engine_stub.py"
    executable = tmp_path / "engine"
    executable.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    executable.chmod(0o755)
    log_path = tmp_path / "commands.log"
    scenario_path = tmp_path / "scenario.json"
    monkeypatch.setenv("UCI_ENGINE_LOG", str(log_path))
    monkeypatch.setenv("UCI_ENGINE_SCENARIO", str(scenario_path))
    return StubEngine(
        executable=executable, log_path=log_path, scenario_path=scenario_path
    )


@pytest.fixture
def scripted_engine(stub_engine: StubEngine) -> StubEngine:
    stub_engine.script(_default_scenario())
    return stub_engine


def _default_scenario() -> dict[str, object]:
    return {
        "name": "StubEngine 2.3",
        "positions": {
            START_FEN: [
                "info depth 15 seldepth 20 multipv 1 score cp 35 nodes 100 nps 1000"
                " pv e2e4 e7e5 g1f3",
                "bestmove e2e4 ponder e7e5",
            ],
            AFTER_E4_FEN: [
                "info depth 15 score cp 40 pv c7c5 g1f3",
                "bestmove c7c5",
            ],
            FOOLS_MATE_FEN: [
                "info depth 0 score mate 0",
                "bestmove (none)",
            ],
        },
    }


def test_evaluates_positions_through_a_single_process_per_operation(
    scripted_engine: StubEngine,
) -> None:
    evaluator = StockfishEvaluator(
        EngineSettings(executable=scripted_engine.executable)
    )

    run = evaluator.evaluate_positions([START_FEN, AFTER_E4_FEN, FOOLS_MATE_FEN])

    assert run.evaluations == [
        PositionEvaluation(
            fen=START_FEN,
            score=EvaluationScore(kind="cp", value=35),
            best_move="e2e4",
            principal_variation=["e2e4", "e7e5", "g1f3"],
        ),
        PositionEvaluation(
            fen=AFTER_E4_FEN,
            score=EvaluationScore(kind="cp", value=-40),
            best_move="c7c5",
            principal_variation=["c7c5", "g1f3"],
        ),
        PositionEvaluation(
            fen=FOOLS_MATE_FEN,
            score=EvaluationScore(kind="mate", value=0),
            best_move=None,
            principal_variation=[],
        ),
    ]
    assert run.provenance == EvaluatorProvenance(
        name="StubEngine 2.3",
        version="2.3",
        depth=15,
        threads=1,
        hash_mb=128,
    )
    assert scripted_engine.commands() == [
        "start",
        "uci",
        "setoption name Threads value 1",
        "setoption name Hash value 128",
        "isready",
        f"position fen {START_FEN}",
        "go depth 15",
        f"position fen {AFTER_E4_FEN}",
        "go depth 15",
        f"position fen {FOOLS_MATE_FEN}",
        "go depth 15",
        "quit",
    ]


def test_applies_configured_depth_threads_and_hash(
    scripted_engine: StubEngine,
) -> None:
    evaluator = StockfishEvaluator(
        EngineSettings(
            executable=scripted_engine.executable, depth=3, threads=4, hash_mb=256
        )
    )

    run = evaluator.evaluate_positions([START_FEN])

    assert run.provenance.depth == 3
    assert run.provenance.threads == 4
    assert run.provenance.hash_mb == 256
    commands = scripted_engine.commands()
    assert "setoption name Threads value 4" in commands
    assert "setoption name Hash value 256" in commands
    assert "go depth 3" in commands


def test_starts_a_fresh_process_for_each_operation(
    scripted_engine: StubEngine,
) -> None:
    evaluator = StockfishEvaluator(
        EngineSettings(executable=scripted_engine.executable)
    )

    first = evaluator.evaluate_positions([START_FEN, AFTER_E4_FEN])
    scripted_engine.script(
        {
            "name": "StubEngine 2.4",
            "positions": {
                START_FEN: [
                    "info depth 15 score mate 2 pv d2d4 d7d5",
                    "bestmove d2d4",
                ],
            },
        }
    )
    second = evaluator.evaluate_positions([START_FEN])

    assert first.evaluations[0].score == EvaluationScore(kind="cp", value=35)
    assert second.evaluations[0].score == EvaluationScore(kind="mate", value=2)
    assert second.provenance.version == "2.4"
    commands = scripted_engine.commands()
    assert commands.count("start") == 2
    assert commands.count("uci") == 2
    assert commands.count("go depth 15") == 3
    assert commands.count("quit") == 2


def test_resolves_stockfish_from_path_when_no_executable_is_configured(
    scripted_engine: StubEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy2(scripted_engine.executable, bin_dir / "stockfish")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    run = StockfishEvaluator(EngineSettings()).evaluate_positions([START_FEN])

    assert run.evaluations[0].best_move == "e2e4"
    assert scripted_engine.commands()[0] == "start"


def test_keeps_the_principal_variation_when_the_final_info_line_lacks_one(
    scripted_engine: StubEngine,
) -> None:
    scripted_engine.script(
        {
            "name": "StubEngine 2.3",
            "positions": {
                START_FEN: [
                    "info depth 14 score cp 30 pv e2e4 c7c5",
                    "info depth 15 score cp 35 currmove d2d4 nps 1000",
                    "bestmove e2e4",
                ],
            },
        }
    )
    evaluator = StockfishEvaluator(
        EngineSettings(executable=scripted_engine.executable)
    )

    run = evaluator.evaluate_positions([START_FEN])

    assert run.evaluations[0].score == EvaluationScore(kind="cp", value=35)
    assert run.evaluations[0].best_move == "e2e4"
    assert run.evaluations[0].principal_variation == ["e2e4", "c7c5"]


def test_missing_stockfish_on_path_raises_an_actionable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    with pytest.raises(StockfishError) as error:
        StockfishEvaluator(EngineSettings()).evaluate_positions([START_FEN])

    assert "No Stockfish executable found on PATH" in str(error.value)
    assert "[engine] path" in str(error.value)


def test_missing_configured_executable_raises_an_actionable_error(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "engines" / "missing-stockfish"

    with pytest.raises(StockfishError) as error:
        StockfishEvaluator(EngineSettings(executable=missing)).evaluate_positions(
            [START_FEN]
        )

    assert "missing-stockfish" in str(error.value)
    assert "[engine] path" in str(error.value)


def test_reports_an_engine_that_exits_during_the_handshake(
    stub_engine: StubEngine,
) -> None:
    stub_engine.script({"name": "StubEngine", "exit_on": "uci"})
    evaluator = StockfishEvaluator(EngineSettings(executable=stub_engine.executable))

    with pytest.raises(StockfishError, match="exited while awaiting"):
        evaluator.evaluate_positions([START_FEN])


def test_reports_a_position_evaluated_without_a_score(
    stub_engine: StubEngine,
) -> None:
    stub_engine.script(
        {"name": "StubEngine 2.3", "positions": {START_FEN: ["bestmove e2e4"]}}
    )
    evaluator = StockfishEvaluator(EngineSettings(executable=stub_engine.executable))

    with pytest.raises(StockfishError, match="reported no score"):
        evaluator.evaluate_positions([START_FEN])
