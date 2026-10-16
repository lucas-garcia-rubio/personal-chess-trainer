"""Next-send HTTP contract for a Position inspected during a demonstration."""
from collections.abc import Callable, Iterator
import json

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from test_trainer_mcp import ToolOpenCode, call
from test_trainer_stream import URL, events
from trainer.application.ports import ResponseControl, TrainerEvent
from trainer.bootstrap import create_app
from trainer.config import Settings


class ContextRuntime(ToolOpenCode):
    def __init__(self) -> None:
        super().__init__()
        self.received: list[dict[str, object]] = []

    def stream(self, session_id: str, system: str, context: str,
               control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
        self.received.append(json.loads(context))
        yield from super().stream(session_id, system, context, control)


def test_next_send_selects_inspected_node_after_trainer_advanced(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    runtime = ContextRuntime()
    with TestClient(create_app(migrated_settings("Lance5500"),
                              lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        first = client.post(URL, data={"base_ply": "0", "message": "Demonstrate",
                                       "stream": "true"}).json()
        assert runtime.ready.wait(5)
        before = call(client, runtime.token, "read_context", {
            "attempt_id": first["id"],
        })["structuredContent"]
        try:
            saved = client.get(URL + "/0").json()
            result = call(client, runtime.token, "play_line", {
                "attempt_id": first["id"], "revision": saved["revision"],
                "active_node_id": None, "moves": ["e2e4", "e7e5", "g1f3"],
            })
            assert not result["isError"]
            demonstrated = client.get(URL + "/0").json()
            inspected = demonstrated["nodes"][0]
            # Reading the old Position never competes for the Trainer's selection.
            assert client.get(URL + "/0/fragment").status_code == 200
            assert client.get(URL + "/0").json()["activeNodeId"] == demonstrated["nodes"][-1]["id"]
        finally:
            runtime.release.set()
        events(client, first["eventsUrl"])
        completed = client.get(URL + "/0").json()
        runtime.ready.clear()
        runtime.release.clear()
        second = client.post(URL, data={
            "base_ply": "0", "message": "Explain the inspected Position", "stream": "true",
            "revision": json.dumps(completed["revision"]),
            "position": json.dumps({"kind": "variation", "node_id": inspected["id"]}),
        })
        assert second.status_code == 202
        assert runtime.ready.wait(5)
        try:
            assert runtime.received[-1]["active_node_id"] == inspected["id"]
            assert runtime.received[-1]["active_position"] == inspected["fen"]
            after = call(client, runtime.token, "read_context", {
                "attempt_id": second.json()["id"],
            })["structuredContent"]
            assert after["game"] == before["game"]
            assert after["analysis"] == before["analysis"]
            assert after["base_ply"] == before["base_ply"] == 0
            saved = client.get(URL + "/0").json()
            assert saved["nodes"] == demonstrated["nodes"]
            assert saved["messages"][:-1] == completed["messages"]
            assert saved["baseFen"] == completed["baseFen"]
        finally:
            runtime.release.set()
        events(client, second.json()["eventsUrl"])
