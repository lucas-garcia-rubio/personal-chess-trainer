from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from threading import Event, RLock
from typing import Protocol, runtime_checkable

from trainer.domain import (
    Analysis,
    Discussion,
    DiscussionMessage,
    EvaluationRun,
    GameMetadata,
    GameSummary,
    VariationMove,
)


class GameIdentityConflict(RuntimeError):
    def __init__(self, origin: str, origin_id: str) -> None:
        super().__init__(f"Game identity conflict for {origin}/{origin_id}")


class DiscussionConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Discussion revision is stale; reload the current Discussion")


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


@dataclass(frozen=True)
class TrainerRuntimeAvailability:
    is_available: bool
    reason: str | None = None

    @classmethod
    def available(cls) -> "TrainerRuntimeAvailability":
        return cls(is_available=True)

    @classmethod
    def unavailable(cls, reason: str) -> "TrainerRuntimeAvailability":
        return cls(is_available=False, reason=reason)


class TrainerRuntime(Protocol):
    def start(self) -> TrainerRuntimeAvailability: ...

    def stop(self) -> None: ...

    def create_session(self) -> str: ...

    def session_compatible(self, session_id: str) -> bool: ...

    def delete_session(self, session_id: str) -> None: ...

    def respond(self, session_id: str, system: str, context: str) -> str: ...

    def stream(
        self, session_id: str, system: str, context: str,
        control: "ResponseControl | None" = None,
    ) -> Iterator["TrainerEvent"]: ...

    def abort(self, session_id: str) -> None: ...


@dataclass(frozen=True)
class TrainerEvent:
    type: str
    text: str = ""


class ResponseControl:
    """Serialize provider dispatch with cancellation, including pre-session stops."""

    def __init__(self) -> None:
        self.lock = RLock()
        self.cancelled = Event()
        self.context_chars = 0


@dataclass(frozen=True)
class ResponseAttempt:
    id: int
    request_id: int
    retry_of: int | None
    revision: int
    status: str
    events: list[dict[str, object]]


@dataclass(frozen=True)
class SessionCheckpoint:
    history: int
    nodes: int
    events: dict[str, int]
    context_chars: int
    version: int = 1


class LocalStorage(Protocol):
    def session_checkpoint(
        self, origin: str, origin_id: str, base_ply: int,
    ) -> SessionCheckpoint | None: ...

    def save_session_checkpoint(
        self, origin: str, origin_id: str, base_ply: int,
        session_id: str, checkpoint: SessionCheckpoint,
    ) -> None: ...

    def adopt_evaluator_line(
        self, origin: str, origin_id: str, base_ply: int, revision: int | None,
        parent_id: int | None, base_fen: str, moves: list[VariationMove],
    ) -> None: ...

    def append_trainer_line(
        self, origin: str, origin_id: str, base_ply: int, attempt_id: int,
        revision: int, active_node_id: int | None, moves: list[VariationMove],
    ) -> None: ...

    def response_request(self, request_id: int) -> str: ...

    def begin_response(
        self, origin: str, origin_id: str, base_ply: int,
        retry_of: int | None = None,
    ) -> ResponseAttempt: ...

    def response_attempts(
        self, origin: str, origin_id: str, base_ply: int,
    ) -> list[ResponseAttempt]: ...

    def record_response_event(self, attempt_id: int, event: TrainerEvent) -> None: ...

    def recover_responses(self) -> None: ...

    def get_game_headers(self, origin: str, origin_id: str) -> dict[str, str]: ...

    def get_discussion_session(
        self, origin: str, origin_id: str, base_ply: int
    ) -> str | None: ...

    def set_discussion_session(
        self, origin: str, origin_id: str, base_ply: int, session_id: str
    ) -> None: ...

    def append_discussion_message(
        self, origin: str, origin_id: str, base_ply: int, message: DiscussionMessage
    ) -> None: ...

    def trainer_consent(self) -> bool: ...

    def set_trainer_consent(self, accepted: bool) -> None: ...

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

    def append_discussion_move(
        self,
        origin: str,
        origin_id: str,
        base_ply: int,
        move: VariationMove,
        revision: int,
        parent_id: int | None,
    ) -> None: ...

    def select_discussion_node(
        self, origin: str, origin_id: str, base_ply: int,
        revision: int, node_id: int | None,
    ) -> None: ...

    def delete_discussion(
        self, origin: str, origin_id: str, base_ply: int, revision: int,
    ) -> None: ...

    def close(self) -> None: ...
