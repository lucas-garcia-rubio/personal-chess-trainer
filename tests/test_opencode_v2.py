from collections.abc import Iterator
import json
from pathlib import Path
import sys

import httpx
import pytest

from trainer.adapters.opencode import OpenCodeModelCatalog, OpenCodeRuntime, OpenCodeSessionClient
from trainer.application.ports import ResponseControl, TrainerEvent
from trainer.config import OpenCodeSettings


def test_model_catalog_reads_cli_output_not_logs_and_rejects_invalid_lines(tmp_path: Path) -> None:
    executable = tmp_path / "opencode"
    executable.write_text(f"#!{sys.executable}\n" + '''
import sys
assert sys.argv[1:] == ["models"]
print("diagnostic", file=sys.stderr)
print("other/trainer\\nopenai/gpt-6.1-sol\\n\\nother/trainer\\nnot a model\\n")
''')
    executable.chmod(0o700)
    assert OpenCodeModelCatalog(OpenCodeSettings(executable=executable)).list_models() == [
        "openai/gpt-6.1-sol", "other/trainer",
    ]


@pytest.mark.parametrize("script", ["raise SystemExit(1)", "import time; time.sleep(5)"])
def test_model_catalog_reports_failed_or_timed_out_command(tmp_path: Path, script: str) -> None:
    executable = tmp_path / "opencode"
    executable.write_text(f"#!{sys.executable}\n{script}\n")
    executable.chmod(0o700)
    with pytest.raises(RuntimeError, match="Could not list OpenCode models"):
        OpenCodeModelCatalog(OpenCodeSettings(executable=executable, startup_timeout_seconds=1)).list_models()


def test_runtime_uses_latest_selected_model_for_existing_session(tmp_path: Path) -> None:
    executable = tmp_path / "opencode"
    log = tmp_path / "requests.jsonl"
    executable.write_text(f"#!{sys.executable}\nLOG = {str(log)!r}\n" + '''
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def reply(self, value):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())
    def do_GET(self):
        if self.path == "/api/info":
            self.reply({"version": "2.2.5"})
        elif self.path == "/api/model/default":
            self.reply({"data": {"id": "default", "providerID": "test"}})
        else:
            assert self.path == "/api/event"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'data: {"type":"server.connected","data":{}}\\n\\n')
            self.wfile.write(b'data: {"type":"session.execution.succeeded","data":{"sessionID":"ses_1"}}\\n\\n')
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with open(LOG, "a") as file:
            file.write(json.dumps({"path": self.path, "body": body}) + "\\n")
        self.reply({"data": {"id": "input"}})
server = HTTPServer(("127.0.0.1", 0), Handler)
print(f"server listening on http://127.0.0.1:{server.server_port}", flush=True)
server.serve_forever()
''')
    executable.chmod(0o700)
    selected: str | None = "openai/gpt-6.1-sol"
    runtime = OpenCodeRuntime(
        OpenCodeSettings(executable=executable, model="openai/gpt-6.1-sol"),
        model_selection=lambda: selected,
    )
    try:
        assert runtime.start().is_available
        assert list(runtime.stream("ses_1", "", "first")) == [TrainerEvent("completed")]
        selected = "other/trainer#fast"
        assert list(runtime.stream("ses_1", "", "next")) == [TrainerEvent("completed")]
    finally:
        runtime.stop()
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        {"path": "/api/session/ses_1/model", "body": {"model": {"providerID": "openai", "id": "gpt-6.1-sol"}}},
        {"path": "/api/session/ses_1/prompt", "body": {"text": "first"}},
        {"path": "/api/session/ses_1/model", "body": {"model": {"providerID": "other", "id": "trainer", "variant": "fast"}}},
        {"path": "/api/session/ses_1/prompt", "body": {"text": "next"}},
    ]


def test_sessions_use_v2_envelopes_location_agent_and_idle_interrupt() -> None:
    paths: list[str] = []

    def server(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "POST" and request.url.path == "/api/session":
            assert json.loads(request.content) == {
                "title": "Chess Discussion", "agent": "trainer",
                "location": {"directory": "/private/trainer"},
            }
            return httpx.Response(200, json={"data": {"id": "ses_1"}})
        if request.method == "GET":
            assert request.url.path == "/api/session/ses_1"
            return httpx.Response(200, json={"data": {
                "id": "ses_1", "agent": "trainer",
                "location": {"directory": "/private/trainer"},
            }})
        if request.method == "DELETE":
            return httpx.Response(204)
        assert request.url.path == "/api/session/ses_1/interrupt"
        return httpx.Response(200, json={"interrupted": False})

    with httpx.Client(base_url="http://fake", transport=httpx.MockTransport(server)) as client:
        adapter = OpenCodeSessionClient(client, "/private/trainer")
        assert adapter.create_session() == "ses_1"
        assert adapter.session_compatible("ses_1")
        adapter.abort("ses_1")
        adapter.delete_session("ses_1")
    assert paths == ["/api/session", "/api/session/ses_1",
                     "/api/session/ses_1/interrupt", "/api/session/ses_1"]


@pytest.mark.parametrize("ending", ["succeeded", "failed", "interrupted", "disconnect"])
def test_v2_stream_filters_private_events_and_reconciles_final_text(ending: str) -> None:
    prompted = False

    def frame(kind: str, **data: object) -> bytes:
        return ("data: " + json.dumps({"type": kind, "data": data}) + "\n\n").encode()

    class Events(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield frame("server.connected")
            assert prompted
            yield frame("session.execution.started", sessionID="ses_1")
            yield frame("session.text.delta", sessionID="other", delta="WRONG_SESSION")
            yield frame("session.reasoning.delta", sessionID="ses_1", delta="HIDDEN")
            yield frame("session.tool.called", sessionID="ses_1", input="SECRET")
            yield frame("session.text.started", sessionID="ses_1", assistantMessageID="a", ordinal=0)
            yield frame("session.text.delta", sessionID="ses_1", assistantMessageID="a", ordinal=0, delta="Try ")
            yield frame("session.text.ended", sessionID="ses_1", assistantMessageID="a", ordinal=0, text="Try Nf3.")
            yield frame("session.text.ended", sessionID="ses_1", assistantMessageID="a", ordinal=0, text="Try Nf3.")
            yield frame("session.step.ended", sessionID="ses_1", assistantMessageID="a")
            if ending != "disconnect":
                yield frame(f"session.execution.{ending}", sessionID="other")
                yield frame(f"session.execution.{ending}", sessionID="ses_1", error="PRIVATE_ERROR")

    def server(request: httpx.Request) -> httpx.Response:
        nonlocal prompted
        if request.method == "GET":
            assert request.url.path == "/api/event"
            return httpx.Response(200, stream=Events())
        assert request.url.path == "/api/session/ses_1/prompt"
        assert json.loads(request.content) == {"text": "discussion context"}
        prompted = True
        return httpx.Response(200, json={"data": {"id": "input"}})

    with httpx.Client(base_url="http://fake", transport=httpx.MockTransport(server)) as client:
        stream = OpenCodeSessionClient(client).stream("ses_1", "instructions", "discussion context")
        assert next(stream) == TrainerEvent("activity", "Trainer is consulting a chess tool.")
        assert next(stream) == TrainerEvent("text", "Try ")
        assert next(stream) == TrainerEvent("text", "Nf3.")
        assert next(stream) == TrainerEvent("step")
        if ending == "disconnect":
            with pytest.raises(RuntimeError, match="disconnected"):
                next(stream)
        else:
            assert list(stream) == [TrainerEvent({"succeeded": "completed"}.get(ending, ending))]


def test_cancelled_stream_does_not_dispatch_prompt() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, text='data: {"type":"server.connected","data":{}}\n\n')

    control = ResponseControl()
    control.cancelled.set()
    with httpx.Client(base_url="http://fake", transport=httpx.MockTransport(server)) as client:
        assert list(OpenCodeSessionClient(client).stream("ses_1", "", "", control)) == [TrainerEvent("interrupted")]


@pytest.mark.parametrize("version, model, available", [
    ("2.2.5", True, True), ("2.2.5", False, False),
    ("1.18.35", True, False), ("2.2.2", True, False),
])
def test_runtime_starts_private_v2_server_with_fixed_basic_username(
    tmp_path: Path, version: str, model: bool, available: bool,
) -> None:
    executable = tmp_path / "opencode"
    executable.write_text(f"#!{sys.executable}\n" + '''
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import sys

assert sys.argv[1:] == ["serve", "--hostname", "127.0.0.1", "--port", "0"]
password = os.environ["OPENCODE_PASSWORD"]
assert password
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        assert self.headers["Authorization"] == "Basic " + base64.b64encode(
            ("opencode:" + password).encode()).decode()
        if self.path == "/api/info":
            result = {"version": VERSION}
        else:
            assert self.path == "/api/model/default"
            result = {"data": {"id": "test", "providerID": "test"} if MODEL else None}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())
server = HTTPServer(("127.0.0.1", 0), Handler)
print(f"server listening on http://127.0.0.1:{server.server_port}", flush=True)
server.serve_forever()
'''.replace("VERSION", repr(version)).replace("MODEL", repr(model)))
    executable.chmod(0o700)
    runtime = OpenCodeRuntime(OpenCodeSettings(executable=executable))
    try:
        result = runtime.start()
        assert result.is_available is available
        if not available:
            assert result.reason is not None
            assert "model" in result.reason if not model else "2.2.3" in result.reason
    finally:
        runtime.stop()
