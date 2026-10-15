from collections.abc import Callable, Iterator
import json
from threading import Event
from typing import Any
import pytest
import httpx

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from test_discussion import _trainer_data
from test_trainer_response import FakeOpenCode
from trainer.application.ports import ResponseControl, TrainerEvent
from trainer.bootstrap import create_app
from trainer.config import Settings


URL = "/analyses/lichess/q7ZvsdUF/discussions"


class StreamingOpenCode(FakeOpenCode):
    def stream(self, session_id: str, system: str, context: str,
               control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
        yield TrainerEvent("text", "Consider ")
        yield TrainerEvent("text", "Nf3.")
        yield TrainerEvent("completed")

    def abort(self, session_id: str) -> None:
        pass


def events(client: TestClient, url: str) -> list[dict[str, Any]]:
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def test_token_deltas_render_as_one_message_and_travel_in_the_event_stream(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    tokens = ["Na ", "posição ", "inicial, "] + ["desenvolva " for _ in range(147)]

    class TokenOpenCode(StreamingOpenCode):
        def stream(self, session_id: str, system: str, context: str,
                   control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
            for token in tokens:
                yield TrainerEvent("text", token)
            yield TrainerEvent("completed")

    with TestClient(create_app(migrated_settings("Lance5500"),
                              lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=TokenOpenCode())) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Qual o melhor lance?",
                                        "stream": "true"}).json()
        raw = events(client, attempt["eventsUrl"])
        assert len([event for event in raw if event["type"] == "text"]) == 150
        fragment = client.get(URL + "/0/fragment").text
        assert fragment.count("Trainer: ") == 1
        assert "".join(tokens) in fragment

        # A replayed burst is rendered in one text update, on the SSE connection
        # itself: clients need no fragment GET for any of these 150 deltas.
        rendered = events(client, attempt["eventsUrl"] + "?render=true")
        assert [event["type"] for event in rendered] == ["running", "text", "completed"]
        assert all("html" in event for event in rendered)
        assert rendered[-1]["html"].count("Trainer: ") == 1
        assert "".join(tokens) in rendered[-1]["html"]
        assert rendered[-1]["sequence"] == raw[-1]["sequence"]
        # Reconnection in the middle of a text block includes its saved prefix.
        resumed = events(client, attempt["eventsUrl"] + "?render=true&after=75")
        assert resumed[0]["sequence"] == 151
        assert "".join(tokens) in resumed[0]["html"]


def test_stream_is_ordered_persisted_and_replayable_after_restart(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=StreamingOpenCode())) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        response = client.post(URL, data={"base_ply": "0", "message": "What now?", "stream": "true"})
        assert response.status_code == 202
        attempt = response.json()
        output = events(client, attempt["eventsUrl"])
        assert [entry["type"] for entry in output] == ["running", "text", "text", "completed"]
        assert [entry["text"] for entry in output[1:3]] == ["Consider ", "Nf3."]
        assert [entry["sequence"] for entry in output] == [1, 2, 3, 4]
        discussion = client.get(URL + "/0").json()
        assert discussion["messages"] == [{"author": "player", "body": "What now?"},
                                           {"author": "trainer", "body": "Consider Nf3."}]
        assert discussion["attempts"][0]["status"] == "completed"

    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"))) as client:
        assert client.get(URL + "/0").json() == discussion
        assert events(client, attempt["eventsUrl"]) == output
        replay = events(client, attempt["eventsUrl"] + "?after=2")
        assert [entry["type"] for entry in replay] == ["text", "completed"]
        assert _trainer_data(client.get("/analyses/lichess/q7ZvsdUF").text)["discussions"] == {"0": discussion}


def test_interrupt_locks_mutations_but_not_reads_and_retry_retains_request_and_partial_text(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    class PausingOpenCode(StreamingOpenCode):
        def __init__(self) -> None:
            super().__init__()
            self.paused = Event()
            self.release = Event()
            self.calls = 0
            self.aborted: list[str] = []

        def stream(self, session_id: str, system: str, context: str,
                   control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
            self.calls += 1
            if self.calls == 1:
                yield TrainerEvent("text", "Confirmed partial.")
                self.paused.set()
                assert self.release.wait(5)
                yield TrainerEvent("text", "MUST_NOT_PERSIST")
            else:
                yield TrainerEvent("text", "Try Nf3.")
            yield TrainerEvent("completed")

        def abort(self, session_id: str) -> None:
            self.aborted.append(session_id)
            self.release.set()

    settings = migrated_settings("Lance5500")
    runtime = PausingOpenCode()
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Help", "stream": "true"}).json()
        assert runtime.paused.wait(5)
        discussion = client.get(URL + "/0").json()
        revision = discussion["revision"]
        assert discussion["attempts"][0]["status"] == "running"
        assert client.post(URL + "/0/moves", json={"revision": revision, "uci": "e2e4"}).status_code == 409
        assert client.post(URL + "/0/selection", json={"revision": revision, "nodeId": None}).status_code == 409
        assert client.request("DELETE", URL + "/0", json={"revision": revision, "confirmed": True}).status_code == 409
        assert client.post(URL, data={"base_ply": "0", "message": "duplicate", "stream": "true"}).status_code == 409
        assert client.get("/analyses/lichess/q7ZvsdUF").status_code == 200
        assert client.get(URL + "/0").json() == discussion
        attempt_url = URL + f'/0/attempts/{attempt["id"]}'
        stopped = client.post(attempt_url + "/interrupt").json()
        assert stopped["attempts"][0]["status"] == "interrupted"
        assert runtime.aborted == ["session-first"]
        assert [e["type"] for e in events(client, attempt["eventsUrl"])] == ["running", "text", "interrupted"]
        retry = client.post(attempt_url + "/retry")
        assert retry.status_code == 202
        assert events(client, retry.json()["eventsUrl"])[-1]["type"] == "completed"
        final = client.get(URL + "/0").json()
        assert final["messages"] == [{"author": "player", "body": "Help"},
                                     {"author": "trainer", "body": "Confirmed partial."},
                                     {"author": "trainer", "body": "Try Nf3."}]
        assert [a["status"] for a in final["attempts"]] == ["interrupted", "completed"]
        assert final["attempts"][1]["retry_of"] == attempt["id"]
        assert final["attempts"][1]["request_id"] == final["attempts"][0]["request_id"]
        history = client.get(URL + "/0/fragment").text
        assert history.count("Trainer: ") == 2
        assert "Confirmed partial." in history and "Try Nf3." in history
        assert client.post(URL + "/0/moves", json={"revision": final["revision"], "uci": "e2e4"}).status_code == 200

    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"))) as client:
        recovered = client.get(URL + "/0").json()
        assert recovered["messages"] == final["messages"]
        assert recovered["attempts"] == final["attempts"]


@pytest.mark.parametrize("failure", ["provider", "disconnect", "steps"])
def test_failure_and_step_limit_are_visible_durable_and_retryable(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock, failure: str,
) -> None:
    class BrokenOpenCode(StreamingOpenCode):
        def __init__(self) -> None:
            super().__init__()
            self.aborts = 0

        def stream(self, session_id: str, system: str, context: str,
                   control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
            yield TrainerEvent("text", "<scr")
            yield TrainerEvent("text", "ipt>SECRET</script>Safe partial.")
            if failure == "provider":
                raise RuntimeError("PRIVATE_PROVIDER_ERROR")
            if failure == "steps":
                for _ in range(20):
                    yield TrainerEvent("step")
                yield TrainerEvent("text", "AFTER_LIMIT")
                yield TrainerEvent("completed")

        def abort(self, session_id: str) -> None:
            self.aborts += 1

    settings = migrated_settings("Lance5500")
    runtime = BrokenOpenCode()
    expected = "limited" if failure == "steps" else "failed"
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Help", "stream": "true"}).json()
        output = events(client, attempt["eventsUrl"])
        expected_events = ["running", "text"] + (["activity"] * 7 if failure == "steps" else []) + [expected]
        assert [e["type"] for e in output] == expected_events
        assert output[1]["text"] == "Safe partial."
        assert "8 agent steps" in output[-1]["text"] if failure == "steps" else "please try again" in output[-1]["text"]
        assert runtime.aborts == 1
        saved = client.get(URL + "/0").json()
        assert saved["attempts"][0]["status"] == expected
        assert saved["messages"][-1]["body"] == "Safe partial."
        assert "SECRET" not in json.dumps(saved)
        assert "PRIVATE_PROVIDER_ERROR" not in json.dumps(saved)
        assert "AFTER_LIMIT" not in json.dumps(saved)
        assert client.get("/").status_code == 200

    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"),
                               trainer_runtime=StreamingOpenCode())) as client:
        assert client.get(URL + "/0").json() == saved
        retried = client.post(URL + f'/0/attempts/{attempt["id"]}/retry').json()
        assert events(client, retried["eventsUrl"])[-1]["type"] == "completed"
        final = client.get(URL + "/0").json()
        assert len([m for m in final["messages"] if m["author"] == "player"]) == 1
        assert final["attempts"][0] == saved["attempts"][0]


@pytest.mark.parametrize("ending", ["completed", "provider", "disconnect", "steps"])
def test_real_http_adapter_subscribes_before_prompt_and_filters_v1_events(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
    ending: str,
) -> None:
    from trainer.adapters.opencode import OpenCodeSessionClient

    paths: list[str] = []
    prompted = Event()

    def frame(kind: str, properties: dict[str, object]) -> bytes:
        return ("data: " + json.dumps({"type": kind, "properties": properties}) + "\n\n").encode()

    class RuntimeEvents(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield frame("server.connected", {})
            assert prompted.is_set()
            yield frame("message.updated", {"info": {"id": "user", "sessionID": "session-first", "role": "user"}})
            yield frame("message.updated", {"info": {"id": "assistant", "sessionID": "session-first", "role": "assistant"}})
            parts: list[dict[str, object]] = [
                {"id": "wrong", "messageID": "assistant", "sessionID": "another", "type": "text", "text": "WRONG_SESSION"},
                {"id": "hidden", "messageID": "assistant", "type": "reasoning", "text": "HIDDEN"},
                {"id": "user-text", "messageID": "user", "type": "text", "text": "USER_ECHO"},
                {"id": "synthetic", "messageID": "assistant", "type": "text", "text": "PROMPT", "synthetic": True},
                {"id": "t1", "messageID": "assistant", "type": "text", "text": "Consider "},
                {"id": "tool1", "messageID": "assistant", "type": "tool", "state": {"status": "running", "input": "SECRET_INPUT"}},
                {"id": "t1", "messageID": "assistant", "type": "text", "text": "Consider Nf3."},
                {"id": "t1", "messageID": "assistant", "type": "text", "text": "Consider Nf3."},
            ]
            for part in parts:
                yield frame("message.part.updated", {"part": {"sessionID": "session-first", **part}})
            yield frame("message.part.delta", {"sessionID": "session-first", "messageID": "assistant", "partID": "t1", "field": "text", "delta": " Develop."})
            yield frame("message.part.updated", {"part": {"sessionID": "session-first", "messageID": "assistant", "id": "t1", "type": "text", "text": "Consider Nf3. Develop."}})
            if ending == "provider":
                yield frame("session.error", {"sessionID": "session-first", "error": {"message": "PRIVATE_PROVIDER_ERROR"}})
                return
            if ending == "disconnect":
                return
            if ending == "steps":
                for index in range(20):
                    yield frame("message.part.updated", {"part": {"id": f"step{index}", "sessionID": "session-first", "messageID": "assistant", "type": "step-finish"}})
            yield frame("session.idle", {"sessionID": "another"})
            yield frame("session.idle", {"sessionID": "session-first"})

    def server(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "session-first"})
        if request.url.path == "/event":
            return httpx.Response(200, stream=RuntimeEvents(), headers={"content-type": "text/event-stream"})
        if request.url.path == "/session/session-first/abort":
            return httpx.Response(200, json=True)
        assert request.url.path == "/session/session-first/prompt_async"
        body = json.loads(request.content)
        assert body["agent"] == "trainer"
        assert "model" not in body
        assert body["parts"][0]["type"] == "text"
        prompted.set()
        return httpx.Response(204)

    class HTTPRuntime(StreamingOpenCode):
        def __init__(self) -> None:
            super().__init__()
            self.adapter = OpenCodeSessionClient(httpx.Client(base_url="http://fake", transport=httpx.MockTransport(server)))

        def create_session(self) -> str:
            return self.adapter.create_session()

        def stream(self, session_id: str, system: str, context: str,
                   control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
            return self.adapter.stream(session_id, system, context, control)

        def abort(self, session_id: str) -> None:
            self.adapter.abort(session_id)

    with TestClient(create_app(migrated_settings("Lance5500"), lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=HTTPRuntime())) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Help", "stream": "true"}).json()
        output = events(client, attempt["eventsUrl"])
        terminal = {"completed": "completed", "steps": "limited"}.get(ending, "failed")
        assert [e["type"] for e in output] == ["running", "text", "activity", "text", "text"] + (["activity"] * 7 if ending == "steps" else []) + [terminal]
        assert [e["text"] for e in output if e["type"] == "text"] == ["Consider ", "Nf3.", " Develop."]
        assert "SECRET_INPUT" not in json.dumps(output)
        assert "PRIVATE_PROVIDER_ERROR" not in json.dumps(output)
        assert paths == ["/session", "/event", "/session/session-first/prompt_async"] + (["/session/session-first/abort"] if ending in {"steps", "disconnect"} else [])


def test_interrupt_before_session_creation_prevents_provider_dispatch(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    class StartingOpenCode(StreamingOpenCode):
        def __init__(self) -> None:
            super().__init__()
            self.starting = Event()
            self.release = Event()
            self.dispatched = False

        def create_session(self) -> str:
            self.starting.set()
            assert self.release.wait(5)
            return "late-session"

        def stream(self, session_id: str, system: str, context: str,
                   control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
            self.dispatched = True
            yield TrainerEvent("text", "UNWANTED")
            yield TrainerEvent("completed")

    runtime = StartingOpenCode()
    settings = migrated_settings("Lance5500")
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Help", "stream": "true"}).json()
        assert runtime.starting.wait(5)
        assert client.post(URL + f'/0/attempts/{attempt["id"]}/interrupt').status_code == 200
        runtime.release.set()
        assert [e["type"] for e in events(client, attempt["eventsUrl"])] == ["running", "interrupted"]
    assert not runtime.dispatched


def test_shutdown_preserves_confirmed_text_as_interrupted_and_unlocks_after_reopen(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    class WaitingOpenCode(StreamingOpenCode):
        def __init__(self) -> None:
            super().__init__()
            self.waiting = Event()
            self.release = Event()

        def stream(self, session_id: str, system: str, context: str,
                   control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
            yield TrainerEvent("text", "Already confirmed.")
            self.waiting.set()
            assert self.release.wait(5)
            yield TrainerEvent("text", "LATE_TEXT")

        def abort(self, session_id: str) -> None:
            self.release.set()

    runtime = WaitingOpenCode()
    settings = migrated_settings("Lance5500")
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Help", "stream": "true"}).json()
        assert runtime.waiting.wait(5)

    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"),
                               trainer_runtime=StreamingOpenCode())) as client:
        saved = client.get(URL + "/0").json()
        assert saved["attempts"][0]["status"] == "interrupted"
        assert saved["messages"][-1]["body"] == "Already confirmed."
        retry = client.post(URL + f'/0/attempts/{attempt["id"]}/retry').json()
        events(client, retry["eventsUrl"])
        final = client.get(URL + "/0").json()
        assert client.request("DELETE", URL + "/0", json={"revision": final["revision"], "confirmed": True}).status_code == 204
        assert client.get(URL + "/0").status_code == 404
