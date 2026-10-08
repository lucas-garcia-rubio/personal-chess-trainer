import json
from typing import cast

from trainer.application.ports import GameIdentityConflict, GameSource, LocalStorage
from trainer.domain import GameDocument, derive_analysis, derive_lichess_metadata


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
            raw_document = raw_document.strip()
            game = cast(GameDocument, json.loads(raw_document))
            metadata = derive_lichess_metadata(game)
            existing = self._storage.get_canonical_document(
                metadata.origin, metadata.origin_id
            )
            if existing is not None:
                if existing != metadata.canonical_document:
                    raise GameIdentityConflict(metadata.origin, metadata.origin_id)
                continue
            analysis = derive_analysis(
                game, self._player_username, played_at=metadata.played_at
            )
            self._storage.save_game(raw_document, metadata, analysis)
