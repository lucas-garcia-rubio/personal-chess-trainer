from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from trainer.application.ports import LocalStorage

_templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def create_web_app(storage: LocalStorage) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        storage.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        return _templates.TemplateResponse(request, "home.html")

    return app
