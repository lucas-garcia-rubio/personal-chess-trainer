from collections.abc import Callable
from threading import Event
from typing import Any, cast
import json
import pytest

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from test_trainer_stream import StreamingOpenCode, URL, events
from trainer.bootstrap import create_app
from trainer.config import Settings


class ToolOpenCode(StreamingOpenCode):
    def __init__(self) -> None:
        super().__init__()
        self.ready = Event()
        self.release = Event()
        self.token = ""

    def configure_mcp(self, url: str, token: str) -> None:
        self.token = token

    def respond(self, session_id: str, system: str, context: str) -> str:
        return "Done."

    def stream(self, session_id: str, system: str, context: str, control: Any = None) -> Any:
        self.ready.set()
        assert self.release.wait(10)
        yield from super().stream(session_id, system, context, control)

    def abort(self, session_id: str) -> None:
        self.release.set()


def call(client: TestClient, token: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/trainer/mcp", headers={"Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response.status_code == 200
    return cast(dict[str, Any], response.json()["result"])


def test_context_authentication_and_run_scoping(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    runtime = ToolOpenCode()
    with TestClient(create_app(migrated_settings("Lance5500"),
                              lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Help", "stream": "true"}).json()
        assert runtime.ready.wait(5)
        try:
            assert client.post("/trainer/mcp", json={}).status_code == 401
            assert client.post("/trainer/mcp", headers={"Authorization": "Bearer wrong"}, json={}).status_code == 401
            headers = {"Authorization": f"Bearer {runtime.token}"}
            initialized = client.post("/trainer/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2025-03-26"}}).json()["result"]
            assert initialized["protocolVersion"] == "2025-03-26"
            tools = client.post("/trainer/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
            assert [tool["name"] for tool in tools] == ["read_context", "play_line"]
            assert tools[1]["inputSchema"]["properties"]["moves"]["maxItems"] == 12
            result = call(client, runtime.token, "read_context", {"attempt_id": attempt["id"]})
            context = result["structuredContent"]
            assert context["game"][1].startswith("d4 d5 c4")
            assert context["history"] == [{"author": "player", "body": "Help"}]
            assert context["variations"] == []
            assert context["active_node_id"] is None
            assert context["revision"] == client.get(URL + "/0").json()["revision"]
            assert "analysis" in context and "critical_moment" in context
            assert runtime.token and runtime.token not in client.get("/analyses/lichess/q7ZvsdUF").text
            assert runtime.token not in json.dumps(client.get(URL + "/0").json())
        finally:
            runtime.release.set()
        events(client, attempt["eventsUrl"])
        assert call(client, runtime.token, "read_context", {"attempt_id": attempt["id"]})["isError"]


@pytest.mark.parametrize("rejection", ["illegal", "limit", "stale", "node"])
def test_atomic_lines_recovery_order_and_restart(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock, rejection: str,
) -> None:
    settings = migrated_settings("Lance5500")
    runtime = ToolOpenCode()
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Demonstrate", "stream": "true"}).json()
        assert runtime.ready.wait(5)
        before = client.get(URL + "/0").json()
        args = {"attempt_id": attempt["id"], "revision": before["revision"], "active_node_id": None,
                "moves": ["e2e4", "e7e5", "g1f3"]}
        bad = dict(args)
        if rejection == "illegal":
            bad["moves"] = ["e2e4", "e7e5", "e4e6"]
        elif rejection == "limit":
            bad["moves"] = ["e2e4"] * 13
        elif rejection == "stale":
            bad["revision"] = before["revision"] - 1
        else:
            bad["active_node_id"] = 99999
        try:
            rejected = call(client, runtime.token, "play_line", bad)
            assert rejected["isError"]
            assert client.get(URL + "/0").json() == before
            current = call(client, runtime.token, "read_context", {"attempt_id": attempt["id"]})["structuredContent"]
            args["revision"] = current["revision"]
            result = call(client, runtime.token, "play_line", args)
            assert not result["isError"]
            during = client.get(URL + "/0").json()
            assert [n["uci"] for n in during["nodes"]] == ["e2e4", "e7e5", "g1f3"]
            assert [n["author"] for n in during["nodes"]] == ["trainer"] * 3
            assert [n["san"] for n in during["nodes"]] == ["e4", "e5", "Nf3"]
            assert during["activeNodeId"] == during["nodes"][-1]["id"]
            assert during["activeFen"] == "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
            refreshed = call(client, runtime.token, "read_context", {"attempt_id": attempt["id"]})["structuredContent"]
            assert refreshed["active_position"] == during["activeFen"]
            assert refreshed["active_node_id"] == during["activeNodeId"]
            assert refreshed["revision"] == before["revision"] + 3
            assert call(client, runtime.token, "play_line", args)["isError"]
            assert client.get(URL + "/0").json() == during
        finally:
            runtime.release.set()
        output = events(client, attempt["eventsUrl"])
        assert [e["type"] for e in output] == ["running", "move", "move", "move", "text", "text", "completed"]
        assert [json.loads(e["text"])["san"] for e in output if e["type"] == "move"] == ["e4", "e5", "Nf3"]
        saved = client.get(URL + "/0").json()
        assert call(client, runtime.token, "play_line", {
            "attempt_id": attempt["id"], "revision": saved["revision"],
            "active_node_id": saved["activeNodeId"], "moves": ["b8c6"],
        })["isError"]
        assert client.get(URL + "/0").json() == saved
    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"))) as client:
        assert client.get(URL + "/0").json() == saved
        assert events(client, attempt["eventsUrl"]) == output
        selected = client.post(URL + "/0/selection", json={"revision": saved["revision"], "nodeId": saved["nodes"][0]["id"]})
        assert selected.status_code == 200


def test_production_runtime_receives_only_approved_tools_and_ephemeral_mcp_configuration(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trainer.adapters.opencode import OpenCodeRuntime
    from trainer.config import OpenCodeSettings

    configurations: list[dict[str, Any]] = []

    def unavailable_process(*args: Any, **kwargs: Any) -> Any:
        configurations.append(json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"]))
        raise OSError("No process in tests")

    monkeypatch.setattr("trainer.adapters.opencode.subprocess.Popen", unavailable_process)
    settings = migrated_settings("Lance5500")
    # An explicit executable reaches the process boundary without requiring OpenCode installed.
    from pathlib import Path
    for _ in range(2):
        runtime = OpenCodeRuntime(OpenCodeSettings(executable=Path("/fake/opencode")))
        with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"),
                                  trainer_runtime=runtime)) as client:
            assert client.get("/").status_code == 200
            config = configurations[-1]
            assert config["permission"] == {"*": "deny"}
            assert config["agent"]["trainer"]["permission"] == {
                "*": "deny", "trainer_read_context": "allow", "trainer_play_line": "allow",
            }
            assert "model" not in config["agent"]["trainer"]
            assert list(config["mcp"]) == ["trainer"]
            assert config["mcp"]["trainer"]["url"].startswith("http://127.0.0.1:")
            assert config["mcp"]["trainer"]["oauth"] is False
    assert configurations[0]["mcp"]["trainer"]["headers"] != configurations[1]["mcp"]["trainer"]["headers"]
