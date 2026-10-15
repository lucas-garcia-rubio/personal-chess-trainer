"""Evaluator evidence is supplied by the application, never by model prose."""
import json
from collections import OrderedDict
from dataclasses import dataclass
import secrets
from typing import cast

import chess

from trainer.application.discussions import get_discussion, node_fen
from trainer.application.ports import CandidateEvaluator, DiscussionConflict, LocalStorage, TrainerEvent
from trainer.domain import Discussion, VariationMove


@dataclass(frozen=True)
class _Preview:
    key: tuple[str, str, int]
    revision: int | None
    parent_id: int | None
    base_fen: str
    starting_fen: str
    prefix: list[VariationMove]
    moves: list[VariationMove]


def validated_line(fen: str, moves: list[str], *, player: bool = False) -> list[VariationMove]:
    board = chess.Board(fen)
    validated = []
    for uci in moves:
        move = board.parse_uci(uci)
        if move not in board.legal_moves:
            raise ValueError("Evaluator line contains an illegal move")
        san = board.san(move)
        board.push(move)
        validated.append(VariationMove("player" if player else "evaluator", uci, san, board.fen()))
    return validated


class BestLineUnavailable(ValueError):
    pass


class EvaluationLimit(ValueError):
    pass


def game_position(storage: LocalStorage, origin: str, origin_id: str, ply: int) -> str:
    canonical = storage.get_canonical_document(origin, origin_id)
    if canonical is None:
        raise ValueError("Game not found")
    game = cast(list[str], json.loads(canonical))
    moves = game[1].split()
    if not 0 <= ply <= len(moves):
        raise ValueError("The selected Game ply is invalid.")
    board = chess.Board(game[0])
    for san in moves[:ply]:
        board.push_san(san)
    return board.fen()


class EvaluatePosition:
    def __init__(self, storage: LocalStorage, evaluator: CandidateEvaluator | None) -> None:
        self.storage = storage
        self.evaluator = evaluator
        # Previews are transient, bounded and opaque; SQLite remains unchanged until adoption.
        self._previews: OrderedDict[str, _Preview] = OrderedDict()

    def _preview(self, key: tuple[str, str, int], preview_id: str | None) -> _Preview:
        preview = self._previews.get(preview_id or "")
        if preview is None or preview.key != key:
            raise ValueError("Preview expired or does not belong to this Base Position")
        return preview

    def preview_positions(self, key: tuple[str, str, int]) -> list[dict[str, object]]:
        return [{"kind": "preview", "preview_id": identifier, "plies": len(preview.moves)}
                for identifier, preview in self._previews.items() if preview.key == key]

    def discard_previews(self, key: tuple[str, str, int]) -> None:
        for identifier, preview in list(self._previews.items()):
            if preview.key == key:
                del self._previews[identifier]

    def consult(self, origin: str, origin_id: str, base_ply: int, *, attempt_id: int,
                kind: str, ply: int | None, node_id: int | None,
                preview_id: str | None = None) -> dict[str, object]:
        fen = self.resolve(origin, origin_id, base_ply, kind=kind, ply=ply,
                           node_id=node_id, preview_id=preview_id)
        attempt = next(a for a in self.storage.response_attempts(origin, origin_id, base_ply)
                       if a.id == attempt_id)
        if attempt.status != "running":
            raise ValueError("Response is no longer active")
        if sum(event["type"] == "evidence" for event in attempt.events) >= 3:
            raise EvaluationLimit
        evidence = self(origin, origin_id, fen)
        self.storage.record_response_event(attempt_id, TrainerEvent("evidence", json.dumps(evidence)))
        return evidence

    def resolve(self, origin: str, origin_id: str, base_ply: int, *,
                kind: str, ply: int | None, node_id: int | None,
                preview_id: str | None = None) -> str:
        if kind == "preview" and ply is not None and node_id is None:
            preview = self._preview((origin, origin_id, base_ply), preview_id)
            if not 0 <= ply <= len(preview.moves):
                raise ValueError("Preview ply is invalid")
            return preview.moves[ply - 1].resulting_fen if ply else preview.starting_fen
        if preview_id is not None:
            raise ValueError("Unexpected preview reference")
        if kind == "game" and ply is not None and node_id is None:
            return game_position(self.storage, origin, origin_id, ply)
        if kind == "variation" and ply is None:
            return node_fen(get_discussion(self.storage, origin, origin_id, base_ply), node_id)
        raise ValueError("Position must reference this Game or Discussion")

    def best_line(self, origin: str, origin_id: str, base_ply: int, *,
                  kind: str, ply: int | None, node_id: int | None,
                  preview_id: str | None = None) -> dict[str, object]:
        key = (origin, origin_id, base_ply)
        base_fen = game_position(self.storage, origin, origin_id, base_ply)
        fen = self.resolve(*key, kind=kind, ply=ply, node_id=node_id, preview_id=preview_id)
        discussion = next((d for d in self.storage.list_discussions(origin, origin_id)
                           if d.base_ply == base_ply), None)
        revision = discussion.revision if discussion else None
        parent_id = node_id if kind == "variation" else None
        prefix: list[VariationMove] = []
        if kind == "game" and ply != base_ply:
            raise ValueError("Preview must start at the selected Base Position")
        if kind == "preview":
            previous = self._preview(key, preview_id)
            revision, parent_id = previous.revision, previous.parent_id
            prefix = previous.prefix + previous.moves[:ply]
        evidence = self(origin, origin_id, fen)
        if evidence["status"] != "verified":
            raise BestLineUnavailable(str(evidence["reason"]))
        best = cast(list[dict[str, object]], evidence["candidates"])[0]
        line = cast(list[dict[str, str]], best["moves"])
        moves = validated_line(fen, [move["uci"] for move in line])
        identifier = secrets.token_urlsafe(24)
        self._previews[identifier] = _Preview(key, revision, parent_id, base_fen, fen, prefix, moves)
        while len(self._previews) > 128:
            self._previews.popitem(last=False)
        return {"previewId": identifier, "label": "Best Line preview", "basePly": base_ply,
                "startingFen": fen, "score": best["score"], "moves": line, "provenance": evidence["provenance"]}

    def adopt(self, origin: str, origin_id: str, base_ply: int, *, preview_id: str,
              plies: int, uci: str | None) -> Discussion:
        key = (origin, origin_id, base_ply)
        preview = self._preview(key, preview_id)
        if not 0 <= plies <= len(preview.moves):
            raise ValueError("Preview ply is invalid")
        moves = preview.prefix + preview.moves[:plies]
        if uci is not None:
            fen = preview.moves[plies - 1].resulting_fen if plies else preview.starting_fen
            moves += validated_line(fen, [uci], player=True)
        if not moves:
            raise ValueError("Select a preview move to adopt")
        # Revalidate the complete path, including Evaluator-supplied moves, before persistence.
        discussion = next((d for d in self.storage.list_discussions(origin, origin_id)
                           if d.base_ply == base_ply), None)
        if (discussion.revision if discussion else None) != preview.revision:
            raise DiscussionConflict
        anchor = node_fen(discussion, preview.parent_id) if discussion else preview.base_fen
        validated_line(anchor, [move.uci for move in moves])
        self.storage.adopt_evaluator_line(*key, preview.revision, preview.parent_id, preview.base_fen, moves)
        return get_discussion(self.storage, *key)

    def adopt_position(self, origin: str, origin_id: str, base_ply: int, *,
                       preview_id: str, plies: int) -> None:
        preview = self._preview((origin, origin_id, base_ply), preview_id)
        if plies or preview.prefix:
            self.adopt(origin, origin_id, base_ply, preview_id=preview_id, plies=plies, uci=None)
            return
        discussion = next((d for d in self.storage.list_discussions(origin, origin_id)
                           if d.base_ply == base_ply), None)
        if (discussion.revision if discussion else None) != preview.revision:
            raise DiscussionConflict
        if discussion:
            self.storage.select_discussion_node(origin, origin_id, base_ply, discussion.revision, preview.parent_id)

    def __call__(self, origin: str, origin_id: str, fen: str) -> dict[str, object]:
        analysis = self.storage.get_analysis(origin, origin_id)
        if analysis is None:
            raise ValueError("Analysis not found")
        unavailable: dict[str, object] = {
            "status": "unavailable", "position": fen, "perspective": "player",
            "candidates": [], "reason": "Evaluator unavailable; chess judgment is unverified.",
        }
        if self.evaluator is None:
            return unavailable
        try:
            run = self.evaluator.evaluate_candidates(fen, 3)
            if not run.evaluations:
                return unavailable
            candidates: list[dict[str, object]] = []
            for evaluation in run.evaluations[:3]:
                if evaluation.fen != fen:
                    raise ValueError("Evaluator returned a different Position")
                moves = [{"uci": move.uci, "san": move.san, "fen": move.resulting_fen}
                         for move in validated_line(fen, evaluation.principal_variation[:12])]
                candidates.append({
                    "score": {"kind": evaluation.score.kind,
                              "value": evaluation.score.value * (-1 if analysis.player_color == "black" else 1)},
                    "moves": moves,
                })
            return {
                "status": "verified", "position": fen, "perspective": "player", "candidates": candidates,
                "provenance": {"sourceKind": run.provenance.source_kind, "name": run.provenance.name,
                               "version": run.provenance.version, "parameters": run.provenance.parameters},
            }
        except Exception:
            # Engine diagnostics are private; failure is explicit and does not stop teaching.
            return unavailable
