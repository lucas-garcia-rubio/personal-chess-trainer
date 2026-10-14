from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from trainer.domain import Analysis, Discussion, EvaluationRun, GameMetadata, GameSummary


class GameIdentityConflict(RuntimeError):
    def __init__(self, origin: str, origin_id: str) -> None:
        super().__init__(f"Game identity conflict for {origin}/{origin_id}")


class GameSource(Protocol):
    def fetch_games(self, since: int | None = None) -> list[str]: ...

    def close(self) -> None: ...


class PositionEvaluator(Protocol):
    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun: ...


@runtime_checkable
class CandidateEvaluator(Protocol):
    def evaluate_candidates(
        self, position: str, max_candidates: int
    ) -> EvaluationRun: ...


class LocalStorage(Protocol):
    def save_game(
        self, raw_document: str, metadata: GameMetadata, analysis: Analysis
    ) -> None: ...

    def get_canonical_document(self, origin: str, origin_id: str) -> str | None: ...

    def latest_played_at(self, origin: str) -> int | None: ...

    def list_games(self) -> list[GameSummary]: ...

    def get_analysis(self, origin: str, origin_id: str) -> Analysis | None: ...

    def list_discussions(self, origin: str, origin_id: str) -> list[Discussion]: ...

    def create_discussion(
        self, origin: str, origin_id: str, discussion: Discussion
    ) -> None: ...

    def close(self) -> None: ...
