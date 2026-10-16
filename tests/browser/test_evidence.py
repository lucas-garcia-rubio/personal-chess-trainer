"""Real browser evidence expansion with controlled Evaluator consultations."""
from collections.abc import Callable
import os
from pathlib import Path
import socket
import subprocess
from threading import Thread
import time
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import uvicorn

from lichess_mock import LichessMock
from test_trainer_mcp import ToolOpenCode, call
from test_trainer_evidence import EvidenceEvaluator
from test_trainer_stream import URL, events
from trainer.application.ports import TrainerEvent
from trainer.bootstrap import create_app
from trainer.config import Settings
from trainer.domain import EvaluationRun


class IntermittentEvaluator(EvidenceEvaluator):
    def evaluate_candidates(self, position: str, max_candidates: int) -> EvaluationRun:
        if len(self.requests) == 2:
            raise RuntimeError("Controlled unavailable Evaluator")
        return super().evaluate_candidates(position, max_candidates)


class ControlledEvidenceRuntime(ToolOpenCode):
    def stream(self, session_id: str, system: str, context: str, control: Any = None) -> Any:
        self.ready.set()
        assert self.release.wait(90)
        yield TrainerEvent("text", "Compare these evaluated Positions.")
        yield TrainerEvent("completed")


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Opt-in real desktop browser")
def test_evidence_disclosures_in_browser(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    runtime = ControlledEvidenceRuntime()
    app = create_app(migrated_settings("Lance5500"),
                     lichess_transport=lichess_mock.games_for("Lance5500"), trainer_runtime=runtime,
                      candidate_evaluator=IntermittentEvaluator())

    @app.post("/__test__/finish-response")
    def finish_response() -> None:
        runtime.release.set()

    with TestClient(app) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Demonstrate", "stream": "true"}).json()
        assert runtime.ready.wait(5)
        try:
            assert not call(client, runtime.token, "evaluate_position", {
                "attempt_id": attempt["id"], "position": {"kind": "game", "ply": 0},
            })["isError"]
            saved = client.get(URL + "/0").json()
            assert not call(client, runtime.token, "play_line", {
                "attempt_id": attempt["id"], "revision": saved["revision"],
                "active_node_id": None, "moves": ["e2e4", "e7e5"],
            })["isError"]
            saved = client.get(URL + "/0").json()
            for node in reversed(saved["nodes"]):
                assert not call(client, runtime.token, "evaluate_position", {
                    "attempt_id": attempt["id"], "position": {"kind": "variation", "node_id": node["id"]},
                })["isError"]
            run_browser(app)
        finally:
            runtime.release.set()
        assert events(client, attempt["eventsUrl"])[-1]["type"] == "completed"


def run_browser(app: FastAPI) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            result = subprocess.run([
                "node", str(Path(__file__).with_name("evidence.cjs")),
                f"http://127.0.0.1:{listener.getsockname()[1]}",
            ], capture_output=True, text=True, timeout=90)
            assert result.returncode == 0, result.stdout + result.stderr
        finally:
            server.should_exit = True
            thread.join(timeout=10)
