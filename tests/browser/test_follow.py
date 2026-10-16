"""Board inspection through a real browser while the Trainer remains gated."""
from collections.abc import Callable, Iterator
import json
import os
from pathlib import Path
import subprocess
from threading import Event

import pytest
from fastapi import FastAPI

from test_trainer_response import FakeOpenCode
from trainer.application.ports import ResponseControl, TrainerEvent


class InspectionRuntime(FakeOpenCode):
    def __init__(self) -> None:
        super().__init__()
        self.release = Event()
        self.token = ""
        self.seeded = False
        self.contexts: list[dict[str, object]] = []

    def configure_mcp(self, url: str, token: str) -> None:
        self.token = token

    def stream(self, session_id: str, system: str, context: str,
               control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
        if not self.seeded:
            self.seeded = True
            yield TrainerEvent("text", "Earlier explanation.")
            yield TrainerEvent("completed")
            return
        self.contexts.append(json.loads(context))
        yield TrainerEvent("text", "Inspect while I demonstrate.")
        assert self.release.wait(90)
        yield TrainerEvent("text", " The demonstration is complete.")
        yield TrainerEvent("completed")

    def abort(self, session_id: str) -> None:
        self.release.set()


@pytest.fixture
def desktop_runtime() -> InspectionRuntime:
    return InspectionRuntime()


@pytest.fixture
def browser_test_routes(desktop_runtime: InspectionRuntime) -> Callable[[FastAPI], None]:
    def install(app: FastAPI) -> None:
        @app.get("/test/runtime")
        def state() -> dict[str, object]:
            return {"token": desktop_runtime.token, "contexts": desktop_runtime.contexts}

        @app.post("/test/release")
        def release() -> None:
            desktop_runtime.release.set()
    return install


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Opt-in real desktop browser")
def test_inspect_follow_and_next_send(desktop_app: str) -> None:
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("follow.cjs")), desktop_app],
        capture_output=True, text=True, timeout=100,
    )
    assert result.returncode == 0, result.stdout + result.stderr
