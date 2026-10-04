from fastapi import FastAPI
import httpx

from trainer.adapters.lichess import LichessGameSource
from trainer.adapters.sqlite import SQLiteStorage
from trainer.application.sync import SyncGames
from trainer.config import Settings
from trainer.web import create_web_app


def create_app(
    settings: Settings,
    *,
    lichess_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    storage = SQLiteStorage(settings.database_path)
    game_source = LichessGameSource(
        settings.lichess_username,
        transport=lichess_transport,
    )
    sync_games = SyncGames(game_source, storage, settings.lichess_username)
    return create_web_app(storage, game_source, sync_games)
