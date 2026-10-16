"""Discussion switching with a real stream and controlled browser transport events."""
from collections.abc import Callable, Iterator
import json
import os
from pathlib import Path
import subprocess
from threading import Event

from fastapi import FastAPI
import pytest

from test_trainer_response import FakeOpenCode
from trainer.application.ports import ResponseControl, TrainerEvent


class SwitchingRuntime(FakeOpenCode):
    def __init__(self, ending: str) -> None:
        super().__init__()
        self.release = Event()
        self.token = ""
        self.contexts: list[dict[str, object]] = []
        self.aborted: list[str] = []
        self.ending = ending

    def create_session(self) -> str:
        self.sessions_created += 1
        return f"session-{self.sessions_created}"

    def configure_mcp(self, url: str, token: str) -> None:
        self.token = token

    def stream(self, session_id: str, system: str, context: str,
               control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
        data = json.loads(context)
        self.contexts.append(data)
        if len(self.contexts) == 1:
            yield TrainerEvent("text", "Earlier origin explanation.")
        elif data["base_ply"] == 0 and len(self.contexts) == 2:
            yield TrainerEvent("text", "Origin demonstration in progress.")
            assert self.release.wait(90)
            if self.ending == "failed":
                raise RuntimeError("Controlled origin failure")
            yield TrainerEvent("text", " Origin demonstration complete.")
        elif data["base_ply"] == 0:
            yield TrainerEvent("text", "Origin retry explanation.")
        else:
            yield TrainerEvent("text", "Destination explanation.")
        yield TrainerEvent("completed")

    def abort(self, session_id: str) -> None:
        self.aborted.append(session_id)
        self.release.set()


@pytest.fixture
def desktop_runtime(ending: str) -> SwitchingRuntime:
    return SwitchingRuntime(ending)


@pytest.fixture
def browser_test_routes(desktop_runtime: SwitchingRuntime) -> Callable[[FastAPI], None]:
    def install(app: FastAPI) -> None:
        @app.get("/test/runtime")
        def state() -> dict[str, object]:
            return {"token": desktop_runtime.token, "contexts": desktop_runtime.contexts,
                    "aborted": desktop_runtime.aborted}

        @app.post("/test/release")
        def release() -> None:
            desktop_runtime.release.set()
    return install


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Opt-in real desktop browser")
@pytest.mark.parametrize(("scenario", "ending"), [
    ("switch", "completed"), ("switch", "failed"), ("switch", "interrupted"),
    ("switch_races", "completed"),
])
def test_switch_discussions_during_demonstration(desktop_app: str, scenario: str, ending: str) -> None:
    result = subprocess.run(
        ["node", str(Path(__file__).with_name(f"{scenario}.cjs")), desktop_app, ending],
        capture_output=True, text=True, timeout=100,
    )
    assert result.returncode == 0, result.stdout + result.stderr
