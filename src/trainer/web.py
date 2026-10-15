import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import parse_qs

import chess
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from trainer.adapters.opencode import OpenCodeRuntime
from trainer.adapters.trainer_mcp import MCPRuntime, TrainerMCP
from trainer.application.ports import (
    CandidateEvaluator,
    DiscussionConflict,
    GameSource,
    LocalStorage,
    TrainerRuntime,
    TrainerRuntimeAvailability,
    ResponseAttempt,
)
from trainer.application.import_game import (
    ImportEvaluationError,
    ImportGame,
    ImportValidationError,
)
from trainer.application.discussions import (
    ContinueDiscussion,
    DeleteDiscussion,
    DiscussionValidationError,
    StartDiscussion,
    SelectDiscussionNode,
    get_discussion,
)
from trainer.application.sync import SyncGames
from trainer.application.responses import DiscussionResponses
from trainer.domain import Analysis, Discussion, DiscussionMessage, win_percent

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


def _discussion_document(discussion: Discussion, attempts: list[ResponseAttempt] | None = None) -> dict[str, object]:
    return {
        "attempts": [asdict(attempt) for attempt in attempts or []],
        "baseFen": discussion.base_fen,
        "revision": discussion.revision,
        "activeNodeId": discussion.active_node_id,
        "nodes": [
            {"id": node.id, "parentId": node.parent_id, "author": node.author,
             "uci": node.uci, "san": node.san, "fen": node.resulting_fen}
            for node in discussion.nodes
        ],
        "activeFen": (
            discussion.moves[-1].resulting_fen
            if discussion.moves
            else discussion.base_fen
        ),
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


class _BestLineRequest(BaseModel):
    ply: int


class _PlayerMoveRequest(BaseModel):
    uci: str
    revision: int
    parentId: int | None = None


class _NodeSelectionRequest(BaseModel):
    revision: int
    nodeId: int | None


class _DeleteDiscussionRequest(BaseModel):
    revision: int
    confirmed: bool


class _ConsentRequest(BaseModel):
    accepted: bool


class _PreviewMove(TypedDict):
    uci: str
    san: str
    fen: str


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


def _best_line_preview(
    navigation: _AnalysisNavigation,
    analysis: Analysis,
    evaluator: CandidateEvaluator,
    ply: int,
) -> dict[str, object]:
    positions = navigation["positions"]
    if ply < 0 or ply >= len(positions):
        raise HTTPException(status_code=422, detail="The selected Game ply is invalid.")

    try:
        run = evaluator.evaluate_candidates(positions[ply]["fen"], 3)
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"Best Line unavailable: {error}",
        ) from error
    if not run.evaluations:
        raise HTTPException(
            status_code=503,
            detail="Best Line unavailable: the Evaluator returned no candidates.",
        )

    best = run.evaluations[0]
    board = chess.Board(positions[ply]["fen"])
    moves: list[_PreviewMove] = []
    try:
        for uci in best.principal_variation:
            move = board.parse_uci(uci)
            san = board.san(move)
            board.push(move)
            moves.append({"uci": uci, "san": san, "fen": board.fen()})
    except ValueError as error:
        raise HTTPException(
            status_code=503,
            detail="Best Line unavailable: the Evaluator returned an illegal line.",
        ) from error

    score_value = best.score.value
    if analysis.player_color == "black":
        score_value = -score_value
    return {
        "label": "Best Line preview",
        "basePly": ply,
        "score": {"kind": best.score.kind, "value": score_value},
        "moves": moves,
        "provenance": {
            "sourceKind": run.provenance.source_kind,
            "name": run.provenance.name,
            "version": run.provenance.version,
            "parameters": run.provenance.parameters,
        },
    }


def create_web_app(
    storage: LocalStorage,
    game_source: GameSource,
    sync_games: SyncGames,
    import_game: ImportGame,
    start_discussion: StartDiscussion,
    trainer_runtime: TrainerRuntime,
    continue_discussion: ContinueDiscussion,
    candidate_evaluator: CandidateEvaluator | None = None,
) -> FastAPI:
    responses = DiscussionResponses(storage, trainer_runtime)
    mcp = TrainerMCP(responses)
    if isinstance(trainer_runtime, MCPRuntime):
        trainer_runtime.configure_mcp("http://127.0.0.1/trainer/mcp", mcp.token)
    trainer_availability = TrainerRuntimeAvailability.unavailable(
        "Trainer runtime has not started."
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal trainer_availability
        storage.recover_responses()
        try:
            if isinstance(trainer_runtime, OpenCodeRuntime):
                trainer_runtime.configure_mcp(mcp.start(), mcp.token)
            trainer_availability = trainer_runtime.start()
        except Exception as error:
            trainer_availability = TrainerRuntimeAvailability.unavailable(
                f"OpenCode failed to start: {error}"
            )
        try:
            yield
        finally:
            responses.shutdown()
            mcp.stop()
            game_source.close()
            storage.close()

    app = FastAPI(lifespan=lifespan)
    app.post("/trainer/mcp")(mcp.handle)

    @app.get("/trainer/consent")
    def trainer_consent() -> dict[str, bool]:
        return {"accepted": storage.trainer_consent()}

    @app.post("/trainer/consent")
    def update_trainer_consent(selection: _ConsentRequest) -> dict[str, bool]:
        storage.set_trainer_consent(selection.accepted)
        return trainer_consent()

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
                        str(discussion.base_ply): _discussion_document(discussion, storage.response_attempts(origin, origin_id, discussion.base_ply))
                        for discussion in storage.list_discussions(origin, origin_id)
                    }
                },
                "best_line_unavailable_reason": (
                    "Best Line unavailable: no Evaluator is configured."
                    if candidate_evaluator is None
                    else None
                ),
                "best_line_url": f"/analyses/{origin}/{origin_id}/best-line",
                "trainer_availability": trainer_availability,
                "trainer_consent": storage.trainer_consent(),
            },
        )

    @app.get("/analyses/{origin}/{origin_id}", response_class=HTMLResponse)
    def analysis(request: Request, origin: str, origin_id: str) -> Response:
        return render_analysis(request, origin, origin_id)

    @app.post("/analyses/{origin}/{origin_id}/discussions")
    async def create_discussion(
        request: Request, origin: str, origin_id: str
    ) -> Response:
        if trainer_availability.is_available and not storage.trainer_consent():
            raise HTTPException(
                status_code=403,
                detail="Trainer consent is required before sending data to OpenCode.",
            )
        fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        attempt = None
        try:
            base_ply = int(fields.get("base_ply", [""])[0])
            initial_moves_document = json.loads(
                fields.get("initial_moves", ["[]"])[0]
            )
            if not isinstance(initial_moves_document, list) or not all(
                isinstance(move, str) for move in initial_moves_document
            ):
                raise ValueError
            existing = any(
                d.base_ply == base_ply
                for d in storage.list_discussions(origin, origin_id)
            )
            message = fields.get("message", [""])[0].strip()
            if not message:
                raise DiscussionValidationError("Message is required")
            with responses.lock:
                if any(a.status == "running" for a in storage.response_attempts(origin, origin_id, base_ply)):
                    raise DiscussionConflict
                start_discussion(
                    origin=origin, origin_id=origin_id, base_ply=base_ply,
                    message=message, initial_moves=initial_moves_document,
                )
                if trainer_availability.is_available:
                    if existing:
                        storage.append_discussion_message(
                            origin, origin_id, base_ply,
                            DiscussionMessage(author="player", body=message),
                        )
                    attempt = responses.start(origin, origin_id, base_ply)
        except DiscussionConflict as error:
            raise HTTPException(status_code=409, detail="Trainer is responding; board changes are locked.") from error
        except (ValueError, json.JSONDecodeError, DiscussionValidationError) as error:
            detail = str(error) or "Invalid Discussion"
            raise HTTPException(status_code=422, detail=detail) from error
        if attempt is not None:
            if fields.get("stream", [""])[0] == "true":
                return JSONResponse(attempt_document(origin, origin_id, base_ply, attempt), status_code=202)
            await run_in_threadpool(responses.wait, attempt.id)
            if find_attempt(origin, origin_id, base_ply, attempt.id).status == "failed":
                raise HTTPException(
                    status_code=503,
                    detail="Trainer response failed. Your Discussion is saved; please try again.",
                )
        return RedirectResponse(
            f"/analyses/{origin}/{origin_id}?trainer=1&ply={base_ply}",
            status_code=303,
        )

    def attempt_document(origin: str, origin_id: str, base_ply: int, attempt: ResponseAttempt) -> dict[str, object]:
        return {**asdict(attempt), "eventsUrl": f"/analyses/{origin}/{origin_id}/discussions/{base_ply}/attempts/{attempt.id}/events"}

    def find_attempt(origin: str, origin_id: str, base_ply: int, attempt_id: int) -> ResponseAttempt:
        attempt = next((a for a in storage.response_attempts(origin, origin_id, base_ply) if a.id == attempt_id), None)
        if attempt is None:
            raise HTTPException(status_code=404, detail="Trainer attempt not found")
        return attempt

    @app.get("/analyses/{origin}/{origin_id}/discussions/{base_ply}")
    def read_discussion(origin: str, origin_id: str, base_ply: int) -> dict[str, object]:
        try:
            discussion = get_discussion(storage, origin, origin_id, base_ply)
        except DiscussionValidationError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return _discussion_document(discussion, storage.response_attempts(origin, origin_id, base_ply))

    @app.get("/analyses/{origin}/{origin_id}/discussions/{base_ply}/attempts/{attempt_id}/events")
    def response_events(request: Request, origin: str, origin_id: str, base_ply: int, attempt_id: int, after: int = 0) -> Response:
        find_attempt(origin, origin_id, base_ply, attempt_id)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid event cursor") from error

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            ticks = 0
            while True:
                attempt = find_attempt(origin, origin_id, base_ply, attempt_id)
                for event in attempt.events:
                    sequence = cast(int, event["sequence"])
                    if sequence > cursor:
                        yield f"id: {sequence}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                        cursor = sequence
                if attempt.status != "running":
                    return
                ticks += 1
                if ticks % 300 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.05)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/analyses/{origin}/{origin_id}/discussions/{base_ply}/attempts/{attempt_id}/interrupt")
    def interrupt_response(origin: str, origin_id: str, base_ply: int, attempt_id: int) -> dict[str, object]:
        attempt = find_attempt(origin, origin_id, base_ply, attempt_id)
        try:
            responses.interrupt(attempt, (origin, origin_id, base_ply))
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail="OpenCode could not confirm interruption. Your partial response is saved as interrupted.",
            ) from error
        return read_discussion(origin, origin_id, base_ply)

    @app.post("/analyses/{origin}/{origin_id}/discussions/{base_ply}/attempts/{attempt_id}/retry")
    def retry_response(origin: str, origin_id: str, base_ply: int, attempt_id: int) -> Response:
        find_attempt(origin, origin_id, base_ply, attempt_id)
        if not storage.trainer_consent():
            raise HTTPException(status_code=403, detail="Trainer consent is required.")
        if not trainer_availability.is_available:
            raise HTTPException(status_code=503, detail="Trainer unavailable.")
        try:
            attempt = responses.start(origin, origin_id, base_ply, attempt_id)
        except DiscussionConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return JSONResponse(attempt_document(origin, origin_id, base_ply, attempt), status_code=202)

    @app.post("/analyses/{origin}/{origin_id}/best-line")
    def best_line(
        origin: str, origin_id: str, selection: _BestLineRequest
    ) -> dict[str, object]:
        stored_analysis = storage.get_analysis(origin, origin_id)
        if stored_analysis is None:
            raise HTTPException(status_code=404, detail="Analysis not found")
        canonical_document = storage.get_canonical_document(origin, origin_id)
        if canonical_document is None:
            raise HTTPException(status_code=404, detail="Game not found")
        if candidate_evaluator is None:
            raise HTTPException(
                status_code=503,
                detail="Best Line unavailable: no Evaluator is configured.",
            )
        navigation = _build_analysis_navigation(canonical_document, stored_analysis)
        return _best_line_preview(
            navigation,
            stored_analysis,
            candidate_evaluator,
            selection.ply,
        )

    @app.post("/analyses/{origin}/{origin_id}/discussions/{base_ply}/moves")
    def continue_existing_discussion(
        origin: str,
        origin_id: str,
        base_ply: int,
        selection: _PlayerMoveRequest,
    ) -> dict[str, object]:
        try:
            parent_id = selection.parentId
            if "parentId" not in selection.model_fields_set:
                parent_id = get_discussion(storage, origin, origin_id, base_ply).active_node_id
            discussion = continue_discussion(
                origin=origin,
                origin_id=origin_id,
                base_ply=base_ply,
                uci=selection.uci,
                revision=selection.revision,
                parent_id=parent_id,
            )
        except DiscussionConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except DiscussionValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return _discussion_document(discussion, storage.response_attempts(origin, origin_id, base_ply))

    @app.post("/analyses/{origin}/{origin_id}/discussions/{base_ply}/selection")
    def select_existing_discussion_node(
        origin: str, origin_id: str, base_ply: int, selection: _NodeSelectionRequest,
    ) -> dict[str, object]:
        try:
            discussion = SelectDiscussionNode(storage)(
                origin=origin, origin_id=origin_id, base_ply=base_ply,
                revision=selection.revision, node_id=selection.nodeId,
            )
        except DiscussionConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except DiscussionValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return _discussion_document(discussion, storage.response_attempts(origin, origin_id, base_ply))

    @app.delete("/analyses/{origin}/{origin_id}/discussions/{base_ply}")
    def delete_existing_discussion(
        origin: str, origin_id: str, base_ply: int, selection: _DeleteDiscussionRequest,
    ) -> Response:
        try:
            DeleteDiscussion(storage)(
                origin=origin, origin_id=origin_id, base_ply=base_ply,
                revision=selection.revision, confirmed=selection.confirmed,
            )
        except DiscussionConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except DiscussionValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return Response(status_code=204)

    @app.get("/analyses/{source_id}", response_class=HTMLResponse)
    def legacy_lichess_analysis(request: Request, source_id: str) -> Response:
        return render_analysis(request, "lichess", source_id)

    return app
