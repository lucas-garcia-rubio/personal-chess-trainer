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
    """The OpenCode V2 HTTP boundary; only visible assistant text leaves it."""

    def __init__(self, client: httpx.Client, directory: str | None = None) -> None:
        self._client = client
        self._directory = directory

    def create_session(self) -> str:
        body: dict[str, object] = {"title": "Chess Discussion", "agent": "trainer"}
        if self._directory is not None:
            body["location"] = {"directory": self._directory}
        response = self._client.post("/api/session", json=body)
        response.raise_for_status()
        document = response.json().get("data")
        session_id = document.get("id") if isinstance(document, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("OpenCode returned no session ID.")
        return session_id

    def session_compatible(self, session_id: str) -> bool:
        response = self._client.get(f"/api/session/{quote(session_id, safe='')}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        document = response.json().get("data")
        return (isinstance(document, dict) and document.get("id") == session_id
                and document.get("agent") == "trainer"
                and (self._directory is None
                     or document.get("location") == {"directory": self._directory}))

    def delete_session(self, session_id: str) -> None:
        response = self._client.delete(f"/api/session/{quote(session_id, safe='')}")
        if response.status_code != 404:
            response.raise_for_status()

    def abort(self, session_id: str) -> None:
        response = self._client.post(f"/api/session/{quote(session_id, safe='')}/interrupt")
        response.raise_for_status()
        document = response.json()
        if not isinstance(document, dict) or not isinstance(document.get("interrupted"), bool):
            raise RuntimeError("OpenCode did not acknowledge interruption.")

    def stream(self, session_id: str, system: str, context: str,
               control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
        control = control or ResponseControl()
        text_by_part: dict[tuple[str, int], str] = {}
        finished_steps: set[str] = set()
        prompted = False
        with self._client.stream("GET", "/api/event", timeout=120.0) as response:
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
                properties = event.get("data", {})
                if kind == "server.connected" and not prompted:
                    with control.lock:
                        if control.cancelled.is_set():
                            yield TrainerEvent("interrupted")
                            return
                        prompt = self._client.post(
                            f"/api/session/{quote(session_id, safe='')}/prompt",
                            json={"text": context},
                        )
                        prompt.raise_for_status()
                        prompted = True
                    continue
                if not prompted or properties.get("sessionID") != session_id:
                    continue
                if kind in {"session.text.started", "session.text.delta", "session.text.ended"}:
                    message_id = properties.get("assistantMessageID")
                    ordinal = properties.get("ordinal")
                    if not isinstance(message_id, str) or not isinstance(ordinal, int):
                        continue
                    key = (message_id, ordinal)
                    previous = text_by_part.get(key, "")
                    if kind == "session.text.started":
                        text_by_part.setdefault(key, "")
                        continue
                    text = properties.get("text" if kind == "session.text.ended" else "delta")
                    if not isinstance(text, str):
                        continue
                    if kind == "session.text.delta":
                        text = previous + text
                    if not text.startswith(previous):
                        raise RuntimeError("OpenCode rewrote confirmed text.")
                    text_by_part[key] = text
                    if text != previous:
                        yield TrainerEvent("text", text[len(previous):])
                elif kind == "session.step.ended":
                    message_id = properties.get("assistantMessageID")
                    if isinstance(message_id, str) and message_id not in finished_steps:
                        finished_steps.add(message_id)
                        yield TrainerEvent("step")
                elif kind in {"session.tool.called", "session.tool.success", "session.tool.failed"}:
                    labels = {"session.tool.called": "Trainer is consulting a chess tool.",
                              "session.tool.success": "Trainer finished consulting a chess tool.",
                              "session.tool.failed": "A Trainer chess tool could not complete."}
                    yield TrainerEvent("activity", labels[kind])
                elif kind in {"session.execution.succeeded", "session.execution.failed",
                              "session.execution.interrupted"}:
                    statuses = {"session.execution.succeeded": "completed",
                                "session.execution.failed": "failed",
                                "session.execution.interrupted": "interrupted"}
                    yield TrainerEvent(statuses[kind])
                    return
        raise RuntimeError("OpenCode stream disconnected.")

    def respond(self, session_id: str, system: str, context: str) -> str:
        text: list[str] = []
        for event in self.stream(session_id, system, context):
            if event.type == "text":
                text.append(event.text)
            elif event.type == "completed":
                return "".join(text)
            elif event.type in {"failed", "interrupted"}:
                break
        raise RuntimeError("OpenCode did not complete an assistant response.")


class OpenCodeRuntime:
    def __init__(self, settings: OpenCodeSettings) -> None:
        self._settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._workspace: tempfile.TemporaryDirectory[str] | None = None
        self._client: httpx.Client | None = None
        self._mcp: dict[str, object] = {}
        self._availability = TrainerRuntimeAvailability.unavailable(
            "OpenCode has not started."
        )

    def configure_mcp(self, url: str, token: str) -> None:
        self._mcp = {"trainer": {"type": "remote", "url": url, "disabled": False,
                                 "codemode": False, "oauth": False,
                                 "headers": {"Authorization": f"Bearer {token}"}}}

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
        username = "opencode"
        password = secrets.token_urlsafe(32)
        environment = os.environ.copy()
        environment.update(
            {
                "OPENCODE_PASSWORD": password,
                "OPENCODE_CONFIG_DIR": str(workspace / "config"),
                "OPENCODE_CONFIG_CONTENT": json.dumps(
                    {
                        "$schema": "https://opencode.ai/config.json",
                        "default_agent": "trainer",
                        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
                        "policies": [
                            {"action": "mcp.use", "resource": "*", "effect": "deny"},
                            {"action": "mcp.use", "resource": "trainer", "effect": "allow"},
                            {"action": "skill.use", "resource": "*", "effect": "deny"},
                            {"action": "plugin.use", "resource": "*", "effect": "deny"},
                        ],
                        "mcp": {"servers": self._mcp},
                        "agents": {
                            "trainer": {
                                **({"model": self._settings.model} if self._settings.model else {}),
                                "description": "Discuss chess Positions with the Player.",
                                "mode": "primary",
                                "steps": 8,
                                "permissions": [
                                    {"action": "*", "resource": "*", "effect": "deny"},
                                    {"action": "trainer_read_context", "resource": "*", "effect": "allow"},
                                    {"action": "trainer_play_line", "resource": "*", "effect": "allow"},
                                    {"action": "trainer_evaluate_position", "resource": "*", "effect": "allow"},
                                ],
                                "system": TRAINER_INSTRUCTIONS,
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

    def session_compatible(self, session_id: str) -> bool:
        return self._session_client().session_compatible(session_id)

    def delete_session(self, session_id: str) -> None:
        self._session_client().delete_session(session_id)

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
        return OpenCodeSessionClient(
            self._client, self._workspace.name if self._workspace is not None else None,
        )

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
                health = client.get("/api/info")
                health.raise_for_status()
                document = health.json()
                if not isinstance(document, dict):
                    return "OpenCode reported an unhealthy server."
                version = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(document.get("version", "")))
                if (version is None or int(version[1]) != 2
                        or tuple(map(int, version.groups())) < (2, 2, 3)):
                    return "OpenCode 2.2.3 or newer within V2 is required. Update the configured executable."

                providers = client.get("/api/model/default")
                providers.raise_for_status()
                provider_document = providers.json()
                defaults = (
                    provider_document.get("data")
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
