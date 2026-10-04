from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import chess
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from trainer.application.ports import GameSource, LocalStorage
from trainer.application.sync import SyncGames

_templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _board_pieces(fen: str) -> tuple[str, ...]:
    board = chess.Board(fen)
    return tuple(
        piece.unicode_symbol() if (piece := board.piece_at(square)) else ""
        for rank in range(7, -1, -1)
        for file in range(8)
        for square in [chess.square(file, rank)]
    )


_templates.env.filters["board_pieces"] = _board_pieces


def create_web_app(
    storage: LocalStorage,
    game_source: GameSource,
    sync_games: SyncGames,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        game_source.close()
        storage.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> Response:
        return _templates.TemplateResponse(
            request,
            "home.html",
            {"games": storage.list_games()},
        )

    @app.post("/sync")
    def sync() -> RedirectResponse:
        sync_games()
        return RedirectResponse("/", status_code=303)

    @app.get("/analyses/{source_id}", response_class=HTMLResponse)
    def analysis(request: Request, source_id: str) -> Response:
        stored_analysis = storage.get_analysis(source_id)
        if stored_analysis is None:
            raise HTTPException(status_code=404, detail="Analysis not found")
        return _templates.TemplateResponse(
            request,
            "analysis.html",
            {"analysis": stored_analysis},
        )

    return app
