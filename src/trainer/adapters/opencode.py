import json
from collections.abc import Iterator
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import subprocess
import tempfile
import time
from urllib.parse import quote

import httpx

from trainer.application.ports import ResponseControl, TrainerEvent, TrainerRuntimeAvailability
from trainer.application.trainer_response import TRAINER_INSTRUCTIONS
from trainer.config import OpenCodeSettings


_LISTENING_PATTERN = re.compile(
    r"(?:opencode )?server listening on http://127\.0\.0\.1:(\d+)"
)


class OpenCodeSessionClient:
    """The OpenCode v1 HTTP boundary; only visible assistant text leaves it."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def create_session(self) -> str:
        response = self._client.post("/session", json={"title": "Chess Discussion"})
        response.raise_for_status()
        document = response.json()
        session_id = document.get("id") if isinstance(document, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("OpenCode returned no session ID.")
        return session_id

    def abort(self, session_id: str) -> None:
        response = self._client.post(f"/session/{quote(session_id, safe='')}/abort")
        response.raise_for_status()
        if response.json() is not True:
            raise RuntimeError("OpenCode did not acknowledge interruption.")

    def stream(self, session_id: str, system: str, context: str,
               control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
        control = control or ResponseControl()
        assistant_ids: set[str] = set()
        text_by_part: dict[str, str] = {}
        visible_parts: set[str] = set()
        tool_states: set[tuple[str, str]] = set()
        finished_steps: set[str] = set()
        prompted = False
        with self._client.stream("GET", "/event", timeout=120.0) as response:
            response.raise_for_status()
            data: list[str] = []
            for line in response.iter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                    continue
                if line or not data:
                    continue
                event = json.loads("\n".join(data))
                data.clear()
                kind = event.get("type")
                properties = event.get("properties", {})
                if kind == "server.connected" and not prompted:
                    with control.lock:
                        if control.cancelled.is_set():
                            yield TrainerEvent("interrupted")
                            return
                        prompt = self._client.post(
                            f"/session/{quote(session_id, safe='')}/prompt_async",
                            json={"agent": "trainer", "system": system,
                                  "parts": [{"type": "text", "text": context}]},
                        )
                        prompt.raise_for_status()
                        prompted = True
                elif kind == "message.updated":
                    info = properties.get("info", {})
                    if info.get("sessionID") != session_id or info.get("role") != "assistant":
                        continue
                    assistant_ids.add(info["id"])
                    if info.get("error"):
                        error = info["error"]
                        yield TrainerEvent("interrupted" if error.get("name") == "MessageAbortedError" else "failed")
                        return
                elif kind == "message.part.updated":
                    part = properties.get("part", {})
                    if part.get("sessionID") != session_id or part.get("messageID") not in assistant_ids:
                        continue
                    part_id = part.get("id")
                    if not isinstance(part_id, str):
                        continue
                    if part.get("type") == "step-finish" and part_id not in finished_steps:
                        finished_steps.add(part_id)
                        yield TrainerEvent("step")
                    elif part.get("type") == "text" and not part.get("synthetic") and not part.get("ignored"):
                        visible_parts.add(part_id)
                        text = part.get("text")
                        if not isinstance(text, str):
                            continue
                        previous = text_by_part.get(part_id, "")
                        if not text.startswith(previous):
                            raise RuntimeError("OpenCode rewrote confirmed text.")
                        text_by_part[part_id] = text
                        if text != previous:
                            yield TrainerEvent("text", text[len(previous):])
                    elif part.get("type") == "tool":
                        state = part.get("state", {}).get("status")
                        if state in {"running", "completed", "error"} and (part_id, state) not in tool_states:
                            tool_states.add((part_id, state))
                            labels = {"running": "Trainer is consulting a chess tool.",
                                      "completed": "Trainer finished consulting a chess tool.",
                                      "error": "A Trainer chess tool could not complete."}
                            yield TrainerEvent("activity", labels[state])
                elif kind == "message.part.delta":
                    # Newer v1 servers split text deltas from part snapshots.
                    part_id = properties.get("partID")
                    delta = properties.get("delta")
                    if (properties.get("sessionID") == session_id
                            and properties.get("messageID") in assistant_ids
                            and part_id in visible_parts and properties.get("field") == "text"
                            and isinstance(delta, str)):
                        text_by_part[part_id] = text_by_part.get(part_id, "") + delta
                        yield TrainerEvent("text", delta)
                elif kind == "session.error" and properties.get("sessionID") == session_id:
                    yield TrainerEvent("failed")
                    return
                elif kind in {"session.idle", "session.status"} and properties.get("sessionID") == session_id:
                    idle = kind == "session.idle" or properties.get("status", {}).get("type") == "idle"
                    if idle and assistant_ids:
                        yield TrainerEvent("completed")
                        return
        raise RuntimeError("OpenCode stream disconnected.")

    def respond(self, session_id: str, system: str, context: str) -> str:
        response = self._client.post(
            f"/session/{quote(session_id, safe='')}/message",
            json={
                "agent": "trainer",
                "system": system,
                "parts": [{"type": "text", "text": context}],
            },
            timeout=120.0,
        )
        response.raise_for_status()
        document = response.json()
        if not isinstance(document, dict):
            raise RuntimeError("Invalid OpenCode response.")
        info = document.get("info")
        parts = document.get("parts")
        if (
            not isinstance(info, dict)
            or info.get("role") != "assistant"
            or info.get("sessionID") != session_id
            or info.get("error")
            or not isinstance(parts, list)
        ):
            raise RuntimeError("OpenCode did not complete an assistant response.")
        return "".join(
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            and not part.get("synthetic")
            and not part.get("ignored")
            and part.get("sessionID", session_id) == session_id
            and part.get("messageID", info.get("id")) == info.get("id")
        )


class OpenCodeRuntime:
    def __init__(self, settings: OpenCodeSettings) -> None:
        self._settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._workspace: tempfile.TemporaryDirectory[str] | None = None
        self._client: httpx.Client | None = None
        self._availability = TrainerRuntimeAvailability.unavailable(
            "OpenCode has not started."
        )

    def start(self) -> TrainerRuntimeAvailability:
        if self._process is not None and self._process.poll() is None:
            return self._availability

        executable = self._resolve_executable()
        if executable is None:
            return self._set_unavailable(
                "OpenCode executable was not found. Configure [opencode] path "
                "or add opencode to PATH."
            )

        self._workspace = tempfile.TemporaryDirectory(prefix="chess-trainer-opencode-")
        workspace = Path(self._workspace.name)
        username = secrets.token_urlsafe(18)
        password = secrets.token_urlsafe(32)
        environment = os.environ.copy()
        environment.update(
            {
                "OPENCODE_SERVER_USERNAME": username,
                "OPENCODE_SERVER_PASSWORD": password,
                "OPENCODE_CONFIG_DIR": str(workspace / "config"),
                "OPENCODE_CONFIG_CONTENT": json.dumps(
                    {
                        "default_agent": "trainer",
                        "agent": {
                            "trainer": {
                                "description": "Discuss chess Positions with the Player.",
                                "mode": "primary",
                                "steps": 8,
                                "permission": {"*": "deny"},
                                "prompt": TRAINER_INSTRUCTIONS,
                            }
                        },
                    }
                ),
                "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                "OPENCODE_DISABLE_CLAUDE_CODE": "1",
                "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            }
        )
        try:
            self._process = subprocess.Popen(
                [
                    str(executable),
                    "--pure",
                    "serve",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    "0",
                ],
                cwd=workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as error:
            return self._set_unavailable(
                f"OpenCode could not be started from {executable}: {error.strerror or error}."
            )

        port = self._read_listening_port()
        if port is None:
            process = self._process
            if process is not None and process.poll() is not None:
                return self._set_unavailable(
                    "OpenCode exited before its server became available. Check the configured executable and OpenCode configuration."
                )
            return self._set_unavailable(
                "OpenCode did not start before the configured startup timeout."
            )

        diagnostic = self._health_diagnostic(port, username, password)
        if diagnostic is not None:
            return self._set_unavailable(diagnostic)

        self._availability = TrainerRuntimeAvailability.available()
        self._client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            auth=(username, password),
            timeout=self._settings.http_timeout_seconds,
        )
        return self._availability

    def create_session(self) -> str:
        return self._session_client().create_session()

    def respond(self, session_id: str, system: str, context: str) -> str:
        return self._session_client().respond(session_id, system, context)

    def stream(self, session_id: str, system: str, context: str,
               control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
        return self._session_client().stream(session_id, system, context, control)

    def abort(self, session_id: str) -> None:
        self._session_client().abort(session_id)

    def _session_client(self) -> OpenCodeSessionClient:
        if self._client is None:
            raise RuntimeError("OpenCode is unavailable.")
        return OpenCodeSessionClient(self._client)

    def stop(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if self._workspace is not None:
            self._workspace.cleanup()
            self._workspace = None

    def _resolve_executable(self) -> Path | None:
        if self._settings.executable is not None:
            return self._settings.executable
        discovered = shutil.which("opencode")
        return Path(discovered) if discovered is not None else None

    def _read_listening_port(self) -> int | None:
        process = self._process
        if process is None or process.stdout is None:
            return None
        deadline = time.monotonic() + self._settings.startup_timeout_seconds
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                if not selector.select(remaining):
                    return None
                line = process.stdout.readline()
                match = _LISTENING_PATTERN.search(line)
                if match is not None:
                    return int(match.group(1))
        return None

    def _health_diagnostic(
        self, port: int, username: str, password: str
    ) -> str | None:
        base_url = f"http://127.0.0.1:{port}"
        try:
            with httpx.Client(
                base_url=base_url,
                auth=(username, password),
                timeout=self._settings.http_timeout_seconds,
            ) as client:
                health = client.get("/global/health")
                health.raise_for_status()
                document = health.json()
                if not isinstance(document, dict) or document.get("healthy") is not True:
                    return "OpenCode reported an unhealthy server."

                providers = client.get("/config/providers")
                providers.raise_for_status()
                provider_document = providers.json()
                defaults = (
                    provider_document.get("default")
                    if isinstance(provider_document, dict)
                    else None
                )
                if not isinstance(defaults, dict) or not defaults:
                    return (
                        "OpenCode has no default model available. Configure a provider "
                        "and default model in OpenCode."
                    )
        except (httpx.HTTPError, ValueError):
            return (
                "OpenCode started but its authenticated health check failed. "
                "Check the OpenCode installation and configuration."
            )
        return None

    def _set_unavailable(self, reason: str) -> TrainerRuntimeAvailability:
        self.stop()
        self._availability = TrainerRuntimeAvailability.unavailable(reason)
        return self._availability
