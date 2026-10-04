import json
from typing import cast

from trainer.application.ports import GameSource, LocalStorage
from trainer.domain import GameDocument, derive_analysis


class SyncGames:
    def __init__(
        self,
        game_source: GameSource,
        storage: LocalStorage,
        player_username: str,
    ) -> None:
        self._game_source = game_source
        self._storage = storage
        self._player_username = player_username

    def __call__(self) -> None:
        for raw_document in self._game_source.fetch_games():
            game = cast(GameDocument, json.loads(raw_document))
            analysis = derive_analysis(game, self._player_username)
            self._storage.save_game(raw_document, analysis)
