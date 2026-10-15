import json
from typing import cast

import chess

from trainer.application.ports import DiscussionConflict, LocalStorage
from trainer.domain import Discussion, DiscussionMessage, VariationMove


class DiscussionValidationError(ValueError):
    pass


class StartDiscussion:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def __call__(
        self,
        *,
        origin: str,
        origin_id: str,
        base_ply: int,
        message: str,
        initial_moves: list[str],
    ) -> None:
        canonical_document = self._storage.get_canonical_document(origin, origin_id)
        if canonical_document is None:
            raise DiscussionValidationError("Game not found")
        if not message.strip():
            raise DiscussionValidationError("Message is required")

        game = cast(list[str], json.loads(canonical_document))
        game_moves = game[1].split()
        if base_ply < 0 or base_ply > len(game_moves):
            raise DiscussionValidationError("Base ply is outside the Game")

        board = chess.Board(game[0])
        for san in game_moves[:base_ply]:
            board.push_san(san)
        base_fen = board.fen()

        moves: list[VariationMove] = []
        for uci in initial_moves:
            try:
                move = chess.Move.from_uci(uci)
            except ValueError as error:
                raise DiscussionValidationError("Initial line contains an invalid move") from error
            if move not in board.legal_moves:
                raise DiscussionValidationError("Initial line contains an illegal move")
            san = board.san(move)
            board.push(move)
            moves.append(
                VariationMove(
                    author="player",
                    uci=uci,
                    san=san,
                    resulting_fen=board.fen(),
                )
            )

        self._storage.create_discussion(
            origin,
            origin_id,
            Discussion(
                base_ply=base_ply,
                base_fen=base_fen,
                messages=[DiscussionMessage(author="player", body=message.strip())],
                moves=moves,
            ),
        )


class ContinueDiscussion:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def __call__(
        self, *, origin: str, origin_id: str, base_ply: int, uci: str,
        revision: int, parent_id: int | None,
    ) -> Discussion:
        discussion = get_discussion(self._storage, origin, origin_id, base_ply)
        if discussion.revision != revision:
            raise DiscussionConflict

        active_fen = node_fen(discussion, parent_id)
        board = chess.Board(active_fen)
        try:
            move = chess.Move.from_uci(uci)
        except ValueError as error:
            raise DiscussionValidationError(
                "Move is invalid at the active Position"
            ) from error
        if move not in board.legal_moves:
            raise DiscussionValidationError("Move is illegal at the active Position")

        san = board.san(move)
        board.push(move)
        persisted_move = VariationMove(
            author="player",
            uci=uci,
            san=san,
            resulting_fen=board.fen(),
        )
        self._storage.append_discussion_move(
            origin, origin_id, base_ply, persisted_move, revision, parent_id
        )
        return get_discussion(self._storage, origin, origin_id, base_ply)


def get_discussion(
    storage: LocalStorage, origin: str, origin_id: str, base_ply: int,
) -> Discussion:
    discussion = next(
        (item for item in storage.list_discussions(origin, origin_id)
         if item.base_ply == base_ply), None,
    )
    if discussion is None:
        raise DiscussionValidationError("Discussion not found")
    return discussion


def node_fen(discussion: Discussion, node_id: int | None) -> str:
    if node_id is None:
        return discussion.base_fen
    for node in discussion.nodes:
        if node.id == node_id:
            return node.resulting_fen
    raise DiscussionValidationError("Node does not belong to this Discussion")


class SelectDiscussionNode:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def __call__(
        self, *, origin: str, origin_id: str, base_ply: int,
        revision: int, node_id: int | None,
    ) -> Discussion:
        discussion = get_discussion(self._storage, origin, origin_id, base_ply)
        if discussion.revision != revision:
            raise DiscussionConflict
        node_fen(discussion, node_id)
        self._storage.select_discussion_node(
            origin, origin_id, base_ply, revision, node_id,
        )
        return get_discussion(self._storage, origin, origin_id, base_ply)


class PlayTrainerLine:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def __call__(self, origin: str, origin_id: str, base_ply: int, *,
                 attempt_id: int, revision: int, active_node_id: int | None,
                 moves: list[str]) -> Discussion:
        discussion = get_discussion(self._storage, origin, origin_id, base_ply)
        if discussion.revision != revision or discussion.active_node_id != active_node_id:
            raise DiscussionConflict
        if not 1 <= len(moves) <= 12:
            raise DiscussionValidationError("A line must contain 1 to 12 plies")
        board = chess.Board(node_fen(discussion, active_node_id))
        validated: list[VariationMove] = []
        for uci in moves:
            try:
                move = chess.Move.from_uci(uci)
            except ValueError as error:
                raise DiscussionValidationError("Invalid move; no moves were saved") from error
            if move not in board.legal_moves:
                raise DiscussionValidationError("Illegal move; no moves were saved")
            san = board.san(move)
            board.push(move)
            validated.append(VariationMove(author="trainer", uci=uci, san=san, resulting_fen=board.fen()))
        self._storage.append_trainer_line(origin, origin_id, base_ply, attempt_id,
                                          revision, active_node_id, validated)
        return get_discussion(self._storage, origin, origin_id, base_ply)


class DeleteDiscussion:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def __call__(
        self, *, origin: str, origin_id: str, base_ply: int,
        revision: int, confirmed: bool,
    ) -> None:
        if not confirmed:
            raise DiscussionValidationError("Confirm deletion of the Discussion")
        self._storage.delete_discussion(origin, origin_id, base_ply, revision)
