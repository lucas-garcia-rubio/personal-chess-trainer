from fastapi import FastAPI
import httpx

from trainer.adapters.lichess import LichessGameSource
from trainer.adapters.sqlite import SQLiteStorage
from trainer.adapters.stockfish import StockfishEvaluator
from trainer.application.import_game import ImportGame
from trainer.application.ports import PositionEvaluator
from trainer.application.sync import SyncGames
from trainer.config import Settings
from trainer.web import create_web_app


def create_app(
    settings: Settings,
    *,
    lichess_transport: httpx.BaseTransport | None = None,
    position_evaluator: PositionEvaluator | None = None,
) -> FastAPI:
    storage = SQLiteStorage(settings.database_path)
    game_source = LichessGameSource(
        settings.lichess_username,
        transport=lichess_transport,
    )
    sync_games = SyncGames(game_source, storage, settings.lichess_username)
    evaluator = (
        StockfishEvaluator(settings.engine)
        if position_evaluator is None
        else position_evaluator
    )
    import_game = ImportGame(
        evaluator,
        storage,
        settings.lichess_username,
        max_plies=settings.engine.max_plies,
    )
    return create_web_app(storage, game_source, sync_games, import_game)
