"""Select the displayed Position and persist its request before starting execution."""
from trainer.application.discussions import StartDiscussion
from trainer.application.evaluation import EvaluatePosition
from trainer.application.ports import DiscussionConflict, LocalStorage, ResponseAttempt
from trainer.application.practical_line import PRACTICAL_LINE_REQUEST
from trainer.application.responses import DiscussionResponses


class SubmitDiscussion:
    def __init__(self, storage: LocalStorage, responses: DiscussionResponses,
                 evaluations: EvaluatePosition) -> None:
        self.storage = storage
        self.responses = responses
        self.evaluations = evaluations

    def __call__(self, origin: str, origin_id: str, base_ply: int, *, message: str,
                 initial_moves: list[str], revision: int | None, kind: str | None = None,
                 ply: int | None = None, node_id: int | None = None,
                 preview_id: str | None = None, moves: list[str] | None = None,
                 dispatch: bool = True) -> ResponseAttempt | None:
        key = (origin, origin_id, base_ply)
        if not message.strip():
            raise ValueError("Message is required")
        with self.responses.lock:
            if any(a.status == "running" for a in self.storage.response_attempts(*key)):
                raise DiscussionConflict
            existing = next((d for d in self.storage.list_discussions(origin, origin_id)
                             if d.base_ply == base_ply), None)
            if existing and not dispatch and kind is None:
                return None
            if kind is not None:
                if (existing.revision if existing else None) != revision:
                    raise DiscussionConflict
                self.evaluations.resolve(*key, kind=kind, ply=ply, node_id=node_id,
                                         preview_id=preview_id, moves=moves)
                if kind in {"game", "exploration"} and ply != base_ply:
                    raise ValueError("Game Position must match the Base Position")
            if initial_moves and (existing or kind not in {None, "game"}):
                raise ValueError("Initial moves are only valid before Discussion creation")
            if kind == "exploration":
                if existing:
                    raise ValueError("Exploration is only valid before Discussion creation")
                initial_moves = moves or []
            if kind == "preview":
                assert preview_id is not None and ply is not None
                self.evaluations.adopt_position(*key, preview_id=preview_id, plies=ply)
                existing = next((d for d in self.storage.list_discussions(origin, origin_id)
                                 if d.base_ply == base_ply), None)
            if existing:
                selected_node = node_id if kind is not None and kind != "preview" else existing.active_node_id
                self.storage.select_and_append_request(*key, existing.revision, selected_node, message.strip())
            else:
                StartDiscussion(self.storage)(origin=origin, origin_id=origin_id, base_ply=base_ply,
                                              message=message, initial_moves=initial_moves)
            return self.responses.start(*key) if dispatch else None


class RequestPracticalLine(SubmitDiscussion):
    def request(self, origin: str, origin_id: str, base_ply: int, *, revision: int | None,
                initial_moves: list[str], kind: str, ply: int | None,
                node_id: int | None, preview_id: str | None,
                moves: list[str] | None = None) -> ResponseAttempt:
        attempt = self(origin, origin_id, base_ply, message=PRACTICAL_LINE_REQUEST,
                       revision=revision, initial_moves=initial_moves, kind=kind, ply=ply,
                       node_id=node_id, preview_id=preview_id, moves=moves)
        assert attempt is not None
        return attempt
