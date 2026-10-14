from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import parse_qs

import chess
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from trainer.application.ports import GameSource, LocalStorage
from trainer.application.import_game import (
    ImportEvaluationError,
    ImportGame,
    ImportValidationError,
)
from trainer.application.discussions import DiscussionValidationError, StartDiscussion
from trainer.application.sync import SyncGames
from trainer.domain import Analysis, Discussion, win_percent

_templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


class _NavigationPosition(TypedDict):
    fen: str
    lastMove: str | None


class _MomentHighlights(TypedDict):
    played: str
    best: str


class _EvaluationPoint(TypedDict):
    positionIndex: int
    winPercent: float


class _AnalysisNavigation(TypedDict):
    positions: list[_NavigationPosition]
    moments: dict[str, _MomentHighlights]
    evaluations: list[_EvaluationPoint]


def _discussion_document(discussion: Discussion) -> dict[str, object]:
    return {
        "baseFen": discussion.base_fen,
        "messages": [
            {"author": message.author, "body": message.body}
            for message in discussion.messages
        ],
        "moves": [
            {
                "author": move.author,
                "uci": move.uci,
                "san": move.san,
                "fen": move.resulting_fen,
            }
            for move in discussion.moves
        ],
    }


def _build_analysis_navigation(
    canonical_document: str, analysis: Analysis
) -> _AnalysisNavigation:
    game = cast(list[str], json.loads(canonical_document))
    board = chess.Board(game[0])
    positions: list[_NavigationPosition] = [
        {"fen": board.fen(), "lastMove": None}
    ]
    highlights: dict[str, _MomentHighlights] = {}
    moments_by_ply = {moment.ply: moment for moment in analysis.critical_moments}

    for ply, san in enumerate(game[1].split(), start=1):
        move = board.parse_san(san)
        moment = moments_by_ply.get(ply)
        if moment is not None:
            highlights[str(ply - 1)] = {
                "played": move.uci(),
                "best": board.parse_san(moment.best).uci(),
            }
        board.push(move)
        positions.append({"fen": board.fen(), "lastMove": move.uci()})

    evaluation_points: list[_EvaluationPoint] = []
    next_evaluation = 0
    player_is_white = analysis.player_color == "white"
    for position_index, position in enumerate(positions):
        if next_evaluation >= len(analysis.evaluations):
            break
        evaluation = analysis.evaluations[next_evaluation]
        if evaluation.fen != position["fen"]:
            continue
        evaluation_points.append(
            {
                "positionIndex": position_index,
                "winPercent": round(
                    win_percent(
                        (evaluation.score.kind, evaluation.score.value),
                        player_is_white,
                    ),
                    1,
                ),
            }
        )
        next_evaluation += 1

    return {
        "positions": positions,
        "moments": highlights,
        "evaluations": evaluation_points,
    }


def create_web_app(
    storage: LocalStorage,
    game_source: GameSource,
    sync_games: SyncGames,
    import_game: ImportGame,
    start_discussion: StartDiscussion,
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

    @app.get("/imports/new", response_class=HTMLResponse)
    def new_import(request: Request) -> Response:
        return _templates.TemplateResponse(
            request,
            "import.html",
            {"pgn": "", "error": None},
        )

    @app.post("/imports")
    async def create_import(request: Request) -> Response:
        fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        pgn = fields.get("pgn", [""])[0]
        try:
            imported = import_game(pgn)
        except (ImportValidationError, ImportEvaluationError) as error:
            return _templates.TemplateResponse(
                request,
                "import.html",
                {"pgn": pgn, "error": str(error)},
                status_code=422,
            )
        return RedirectResponse(
            f"/analyses/{imported.origin}/{imported.origin_id}", status_code=303
        )

    def render_analysis(request: Request, origin: str, origin_id: str) -> Response:
        stored_analysis = storage.get_analysis(origin, origin_id)
        if stored_analysis is None:
            raise HTTPException(status_code=404, detail="Analysis not found")
        canonical_document = storage.get_canonical_document(origin, origin_id)
        if canonical_document is None:
            raise HTTPException(status_code=404, detail="Game not found")
        return _templates.TemplateResponse(
            request,
            "analysis.html",
            {
                "analysis": stored_analysis,
                "origin": origin,
                "origin_id": origin_id,
                "navigation": _build_analysis_navigation(
                    canonical_document, stored_analysis
                ),
                "trainer_data": {
                    "discussions": {
                        str(discussion.base_ply): _discussion_document(discussion)
                        for discussion in storage.list_discussions(origin, origin_id)
                    }
                },
            },
        )

    @app.get("/analyses/{origin}/{origin_id}", response_class=HTMLResponse)
    def analysis(request: Request, origin: str, origin_id: str) -> Response:
        return render_analysis(request, origin, origin_id)

    @app.post("/analyses/{origin}/{origin_id}/discussions")
    async def create_discussion(
        request: Request, origin: str, origin_id: str
    ) -> Response:
        fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        try:
            base_ply = int(fields.get("base_ply", [""])[0])
            initial_moves_document = json.loads(
                fields.get("initial_moves", ["[]"])[0]
            )
            if not isinstance(initial_moves_document, list) or not all(
                isinstance(move, str) for move in initial_moves_document
            ):
                raise ValueError
            start_discussion(
                origin=origin,
                origin_id=origin_id,
                base_ply=base_ply,
                message=fields.get("message", [""])[0],
                initial_moves=initial_moves_document,
            )
        except (ValueError, json.JSONDecodeError, DiscussionValidationError) as error:
            detail = str(error) or "Invalid Discussion"
            raise HTTPException(status_code=422, detail=detail) from error
        return RedirectResponse(
            f"/analyses/{origin}/{origin_id}?trainer=1&ply={base_ply}",
            status_code=303,
        )

    @app.get("/analyses/{source_id}", response_class=HTMLResponse)
    def legacy_lichess_analysis(request: Request, source_id: str) -> Response:
        return render_analysis(request, "lichess", source_id)

    return app
