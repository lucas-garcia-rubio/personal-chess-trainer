from collections.abc import Sequence
from typing import Protocol

from trainer.domain import Analysis, EvaluationRun, GameSummary


class GameSource(Protocol):
    def fetch_games(self) -> list[str]: ...

    def close(self) -> None: ...


class PositionEvaluator(Protocol):
    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun: ...


class LocalStorage(Protocol):
    def save_game(self, raw_document: str, analysis: Analysis) -> None: ...

    def list_games(self) -> list[GameSummary]: ...

    def get_analysis(self, source_id: str) -> Analysis | None: ...

    def close(self) -> None: ...
