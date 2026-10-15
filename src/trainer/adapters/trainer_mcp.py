"""Stateless MCP Streamable HTTP boundary. Tools delegate to application cases."""
import json
import secrets
import socket
from threading import Thread
import time
from typing import Any, Protocol, runtime_checkable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from trainer.application.discussions import PlayTrainerLine
from trainer.application.ports import DiscussionConflict
from trainer.application.responses import DiscussionResponses
from trainer.application.trainer_response import ReadDiscussionContext


@runtime_checkable
class MCPRuntime(Protocol):
    def configure_mcp(self, url: str, token: str) -> None: ...


class ContextArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    attempt_id: int


class LineArguments(ContextArguments):
    revision: int
    active_node_id: int | None
    moves: list[str] = Field(min_length=1, max_length=12)


class TrainerMCP:
    def __init__(self, responses: DiscussionResponses) -> None:
        self.responses = responses
        self.token = secrets.token_urlsafe(32)
        self.app = FastAPI()
        self.app.post("/")(self.handle)
        self._server: uvicorn.Server | None = None
        self._worker: Thread | None = None
        self._socket: socket.socket | None = None

    def start(self) -> str:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self._socket = listener
        server = uvicorn.Server(uvicorn.Config(self.app, log_level="error", lifespan="off"))
        self._server = server
        worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        self._worker = worker
        worker.start()
        deadline = time.monotonic() + 5
        while not server.started:
            if not worker.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("Trainer MCP could not start")
            time.sleep(0.01)
        return f"http://127.0.0.1:{listener.getsockname()[1]}/"

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._worker is not None:
            self._worker.join(timeout=5)
        if self._socket is not None:
            self._socket.close()

    async def handle(self, request: Request) -> Response:
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, f"Bearer {self.token}"):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
            method = body.get("method")
            if method == "notifications/initialized":
                return Response(status_code=202)
            result: dict[str, Any]
            if method == "initialize":
                result = {"protocolVersion": body.get("params", {}).get("protocolVersion", "2025-03-26"),
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "chess-trainer", "version": "1.0"}}
            elif method == "tools/list":
                result = {"tools": [
                    {"name": "read_context", "description": "Read authoritative context. Reread after a conflict and retry with its revision and active node.",
                     "inputSchema": ContextArguments.model_json_schema()},
                    {"name": "play_line", "description": "Demonstrate 1-12 legal UCI plies atomically from the current active node and revision.",
                     "inputSchema": LineArguments.model_json_schema()},
                ]}
            elif method == "tools/call":
                try:
                    params = body["params"]
                    args = params["arguments"]
                    with self.responses.lock:
                        if params["name"] == "read_context":
                            parsed = ContextArguments.model_validate(args)
                        elif params["name"] == "play_line":
                            parsed = LineArguments.model_validate(args)
                        else:
                            raise ValueError("Unknown tool")
                        key = self.responses.running_key(parsed.attempt_id)
                        if isinstance(parsed, LineArguments):
                            PlayTrainerLine(self.responses.storage)(*key, **parsed.model_dump())
                        context = ReadDiscussionContext(self.responses.storage)(*key)
                    result = {"content": [{"type": "text", "text": json.dumps(context)}],
                              "structuredContent": context, "isError": False}
                except DiscussionConflict:
                    result = {"content": [{"type": "text", "text": "Revision or active node conflict. Call read_context and retry; no moves were saved."}], "isError": True}
                except (ValueError, KeyError):
                    result = {"content": [{"type": "text", "text": "Invalid line, arguments or inactive response; no moves were saved. Read context and retry."}], "isError": True}
            else:
                return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"),
                                     "error": {"code": -32601, "message": "Method not found"}})
            return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": result})
        except (ValueError, TypeError, AttributeError):
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "Invalid request"}})
