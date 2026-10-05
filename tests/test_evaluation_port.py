from collections.abc import Sequence

from trainer.application.ports import PositionEvaluator
from trainer.domain import (
    EvaluationRun,
    EvaluationScore,
    EvaluatorProvenance,
    PositionEvaluation,
)

STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class DeterministicEvaluator:
    """A Stockfish-free evaluator proving the port accepts deterministic doubles."""

    def __init__(self, score: EvaluationScore, provenance: EvaluatorProvenance) -> None:
        self._score = score
        self._provenance = provenance

    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun:
        return EvaluationRun(
            provenance=self._provenance,
            evaluations=[
                PositionEvaluation(
                    fen=fen,
                    score=self._score,
                    best_move=None,
                    principal_variation=[],
                )
                for fen in positions
            ],
        )


def test_the_evaluation_port_accepts_deterministic_evaluators() -> None:
    evaluator: PositionEvaluator = DeterministicEvaluator(
        EvaluationScore(kind="cp", value=15),
        EvaluatorProvenance(name="deterministic", version=None, depth=1, threads=1, hash_mb=1),
    )

    run = evaluator.evaluate_positions([STARTING_FEN])

    assert run.provenance.name == "deterministic"
    assert run.evaluations == [
        PositionEvaluation(
            fen=STARTING_FEN,
            score=EvaluationScore(kind="cp", value=15),
            best_move=None,
            principal_variation=[],
        )
    ]
