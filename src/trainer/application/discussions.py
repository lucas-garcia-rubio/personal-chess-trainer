import json
from typing import cast

import chess

from trainer.application.ports import LocalStorage
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
        self, *, origin: str, origin_id: str, base_ply: int, uci: str
    ) -> Discussion:
        discussion = next(
            (
                item
                for item in self._storage.list_discussions(origin, origin_id)
                if item.base_ply == base_ply
            ),
            None,
        )
        if discussion is None:
            raise DiscussionValidationError("Discussion not found")

        active_fen = (
            discussion.moves[-1].resulting_fen
            if discussion.moves
            else discussion.base_fen
        )
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
            origin, origin_id, base_ply, persisted_move
        )
        return Discussion(
            base_ply=discussion.base_ply,
            base_fen=discussion.base_fen,
            messages=discussion.messages,
            moves=[*discussion.moves, persisted_move],
        )
