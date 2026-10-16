"""Durable isolation at the public Discussion, Trainer-tool and replay boundaries."""
from collections.abc import Callable, Iterator
import json
from threading import Event

from fastapi.testclient import TestClient
import pytest

from lichess_mock import LichessMock
from test_trainer_evidence import EvidenceEvaluator
from test_trainer_mcp import call
from test_trainer_response import FakeOpenCode
from test_trainer_stream import URL, events
from trainer.application.ports import ResponseControl, TrainerEvent
from trainer.bootstrap import create_app
from trainer.config import Settings


class IsolatedRuntime(FakeOpenCode):
    def __init__(self, ending: str) -> None:
        super().__init__()
        self.ending = ending
        self.ready = Event()
        self.release = Event()
        self.token = ""
        self.origin_started = False
        self.aborted: list[str] = []

    def configure_mcp(self, url: str, token: str) -> None:
        self.token = token

    def create_session(self) -> str:
        self.sessions_created += 1
        return f"session-{self.sessions_created}"

    def stream(self, session_id: str, system: str, context: str,
               control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
        if json.loads(context)["base_ply"] == 0 and not self.origin_started:
            self.origin_started = True
            yield TrainerEvent("text", "Origin partial.")
            self.ready.set()
            assert self.release.wait(10)
            if self.ending == "failed":
                raise RuntimeError("Controlled origin failure")
            yield TrainerEvent("text", " Origin ending.")
        else:
            yield TrainerEvent("text", "Separate explanation.")
        yield TrainerEvent("completed")

    def abort(self, session_id: str) -> None:
        self.aborted.append(session_id)
        self.release.set()


@pytest.mark.parametrize("ending", ["completed", "interrupted", "failed"])
def test_background_response_recovery_and_retry_remain_in_origin(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock, ending: str,
) -> None:
    settings = migrated_settings("Lance5500")
    runtime = IsolatedRuntime(ending)
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=runtime, candidate_evaluator=EvidenceEvaluator())) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        origin = client.post(URL, data={"base_ply": "0", "message": "Origin question",
                                       "initial_moves": '["e2e4"]', "stream": "true"}).json()
        assert runtime.ready.wait(5)
        try:
            other = client.post(URL, data={"base_ply": "1", "message": "Destination question",
                                          "stream": "true"})
            assert other.status_code == 202
            assert events(client, other.json()["eventsUrl"])[-1]["type"] == "completed"
            destination = client.get(URL + "/1").json()
            assert runtime.aborted == []
            saved = client.get(URL + "/0").json()
            assert saved["attempts"][0]["status"] == "running"
            assert not call(client, runtime.token, "play_line", {
                "attempt_id": origin["id"], "revision": saved["revision"],
                "active_node_id": saved["activeNodeId"], "moves": ["e7e5", "g1f3"],
            })["isError"]
            assert not call(client, runtime.token, "evaluate_position", {
                "attempt_id": origin["id"], "position": {"kind": "game", "ply": 0},
            })["isError"]
            before_end = client.get(URL + "/0").json()
            assert before_end["evidence"][0]["status"] == "verified"
            cursor = before_end["attempts"][0]["events"][-1]["sequence"]
            bounded = client.get(URL + f'/0/fragment?attempt_id={origin["id"]}&cursor={cursor}').text
            if ending == "interrupted":
                assert client.post(URL + f'/0/attempts/{origin["id"]}/interrupt').status_code == 200
            else:
                runtime.release.set()
            output = events(client, origin["eventsUrl"])
            assert output[-1]["type"] == ending
            replay = events(client, origin["eventsUrl"] + f"?after={cursor}&render=true")
            assert replay[-1]["type"] == ending
            assert "Origin partial." in replay[-1]["html"]
            assert replay[-1]["html"].count("Trainer: ") == 1
            assert replay[-1]["html"].count('aria-label="Evaluator consultation"') == 1
            assert "Destination question" not in replay[-1]["html"]
            assert client.get(URL + f'/0/fragment?attempt_id={origin["id"]}&cursor={cursor}').text == bounded
            assert client.get(URL + f'/1/attempts/{origin["id"]}/events').status_code == 404
            final = client.get(URL + "/0").json()
            assert final["nodes"] == before_end["nodes"]
            assert final["evidence"] == before_end["evidence"]
            assert final["attempts"][0]["status"] == ending
            assert client.get(URL + "/1").json() == destination
            if ending != "completed":
                assert "Origin ending." not in json.dumps(final)
                retry = client.post(URL + f'/0/attempts/{origin["id"]}/retry')
                assert retry.status_code == 202
                assert events(client, retry.json()["eventsUrl"])[-1]["type"] == "completed"
                retried = client.get(URL + "/0").json()
                assert retried["attempts"][0] == final["attempts"][0]
                assert retried["attempts"][1]["retry_of"] == origin["id"]
                assert retried["nodes"] == final["nodes"]
                fragment = client.get(URL + "/0/fragment").text
                assert fragment.count("Trainer: ") == 2
                assert "New attempt" in fragment
                final = retried
            assert client.get(URL + "/1").json() == destination
        finally:
            runtime.release.set()

    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"))) as client:
        assert client.get(URL + "/0").json() == final
        assert client.get(URL + "/1").json() == destination
        assert events(client, origin["eventsUrl"] + f"?after={cursor}&render=true") == replay
