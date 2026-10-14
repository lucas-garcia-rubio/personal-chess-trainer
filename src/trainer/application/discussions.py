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
