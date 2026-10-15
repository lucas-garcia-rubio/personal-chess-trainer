from fastapi import FastAPI
import httpx

from trainer.adapters.lichess import LichessGameSource
from trainer.adapters.opencode import OpenCodeRuntime
from trainer.adapters.sqlite import SQLiteStorage
from trainer.adapters.stockfish import StockfishEvaluator
from trainer.application.discussions import ContinueDiscussion
from trainer.application.import_game import ImportGame
from trainer.application.ports import CandidateEvaluator, PositionEvaluator, TrainerRuntime
from trainer.application.sync import SyncGames
from trainer.config import Settings
from trainer.web import create_web_app


def create_app(
    settings: Settings,
    *,
    lichess_transport: httpx.BaseTransport | None = None,
    position_evaluator: PositionEvaluator | None = None,
    candidate_evaluator: CandidateEvaluator | None = None,
    trainer_runtime: TrainerRuntime | None = None,
) -> FastAPI:
    storage = SQLiteStorage(settings.database_path)
    game_source = LichessGameSource(
        settings.lichess_username,
        transport=lichess_transport,
    )
    sync_games = SyncGames(game_source, storage, settings.lichess_username)
    evaluator: PositionEvaluator
    best_line_evaluator: CandidateEvaluator | None
    evaluator_unavailable_reason: str | None = None
    if position_evaluator is None:
        stockfish = StockfishEvaluator(settings.engine)
        evaluator = stockfish
        best_line_evaluator = candidate_evaluator or stockfish
        if candidate_evaluator is None:
            evaluator_unavailable_reason = stockfish.unavailable_reason()
            if evaluator_unavailable_reason:
                best_line_evaluator = None
    else:
        evaluator = position_evaluator
        best_line_evaluator = candidate_evaluator
        if best_line_evaluator is None and isinstance(evaluator, CandidateEvaluator):
            best_line_evaluator = evaluator
    import_game = ImportGame(
        evaluator,
        storage,
        settings.lichess_username,
        max_plies=settings.engine.max_plies,
    )
    return create_web_app(
        storage,
        game_source,
        sync_games,
        import_game,
        continue_discussion=ContinueDiscussion(storage),
        candidate_evaluator=best_line_evaluator,
        evaluator_unavailable_reason=evaluator_unavailable_reason,
        trainer_runtime=trainer_runtime or OpenCodeRuntime(settings.opencode),
    )
