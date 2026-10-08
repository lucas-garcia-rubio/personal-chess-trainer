from collections.abc import Sequence
from typing import Protocol

from trainer.domain import Analysis, EvaluationRun, GameMetadata, GameSummary


class GameIdentityConflict(RuntimeError):
    def __init__(self, origin: str, origin_id: str) -> None:
        super().__init__(f"Game identity conflict for {origin}/{origin_id}")


class GameSource(Protocol):
    def fetch_games(self) -> list[str]: ...

    def close(self) -> None: ...


class PositionEvaluator(Protocol):
    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun: ...


class LocalStorage(Protocol):
    def save_game(
        self, raw_document: str, metadata: GameMetadata, analysis: Analysis
    ) -> None: ...

    def get_canonical_document(self, origin: str, origin_id: str) -> str | None: ...

    def list_games(self) -> list[GameSummary]: ...

    def get_analysis(self, origin: str, origin_id: str) -> Analysis | None: ...

    def close(self) -> None: ...
