"""Reusable live application with controlled external services for browser tests."""
from collections.abc import Callable, Iterator
import socket
from threading import Thread
import time

from fastapi import FastAPI
import httpx
import pytest
import uvicorn

from lichess_mock import LichessMock
from test_trainer_response import FakeOpenCode
from trainer.bootstrap import create_app
from trainer.config import Settings


class LongResponseRuntime(FakeOpenCode):
    def respond(self, session_id: str, system: str, context: str) -> str:
        return "\n\n".join(
            f"Position {index}: develop the pieces and compare the open files. " * 8
            for index in range(80)
        ) + "\n\nEnd of the controlled explanation."


@pytest.fixture
def desktop_runtime() -> FakeOpenCode:
    return LongResponseRuntime()


@pytest.fixture
def browser_test_routes() -> Callable[[FastAPI], None]:
    return lambda app: None


@pytest.fixture
def desktop_app(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
    desktop_runtime: FakeOpenCode, browser_test_routes: Callable[[FastAPI], None],
) -> Iterator[str]:
    """Real HTTP server, isolated DB, controlled external services, ephemeral port."""
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500"),
        trainer_runtime=desktop_runtime,
    )
    browser_test_routes(app)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    pytest.fail("Browser application did not start")
                time.sleep(0.01)
            with httpx.Client(base_url=url, timeout=30, follow_redirects=True) as client:
                client.post("/sync").raise_for_status()
                client.post("/analyses/lichess/q7ZvsdUF/discussions", data={
                    "base_ply": "0", "message": "Explain this position at length.",
                    "initial_moves": '["e2e4", "e7e5", "g1f3"]',
                }).raise_for_status()
            yield url
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "Browser server did not stop"
