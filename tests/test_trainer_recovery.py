from collections.abc import Callable
import json
from typing import Any, cast

import httpx

from fastapi.testclient import TestClient
import pytest

from lichess_mock import LichessMock
from test_trainer_response import FakeOpenCode
from test_trainer_stream import URL
from trainer.bootstrap import create_app
from trainer.config import Settings


class RecoverableOpenCode(FakeOpenCode):
    def __init__(self) -> None:
        super().__init__()
        self.sessions: set[str] = set()
        self.deleted: list[str] = []
        self.cleanup_fails = False

    def create_session(self) -> str:
        self.sessions_created += 1
        session = f"session-{self.sessions_created}"
        self.sessions.add(session)
        return session

    def session_compatible(self, session_id: str) -> bool:
        return session_id in self.sessions

    def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)
        if self.cleanup_fails:
            raise RuntimeError("Unavailable")
        self.sessions.discard(session_id)

    def context(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.requests[-1].content)["context"])


def send(client: TestClient, body: str, moves: str = "[]") -> dict[str, Any]:
    response = client.post(URL, data={"base_ply": "0", "message": body, "initial_moves": moves})
    assert response.status_code == 200
    return cast(dict[str, Any], client.get(URL + "/0").json())


def test_recovery_rebuilds_authoritative_context_and_existing_session_gets_only_deltas(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    runtime = RecoverableOpenCode()
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        first = send(client, "Explain development", '["e2e4"]')
        initial = runtime.context()
        branch = client.post(URL + "/0/moves", json={
            "revision": first["revision"], "parentId": None, "uci": "d2d4",
        }).json()
        second = send(client, "Compare my alternative")
        delta = runtime.context()
        assert runtime.sessions_created == 1
        assert delta["context_delivery"] == "delta"
        assert "game" not in delta and "analysis" not in delta
        assert delta["history"] == [{"author": "player", "body": "Compare my alternative"}]
        assert [node["san"] for node in delta["variations"]] == ["d4"]
        assert delta["active_position"] == branch["activeFen"]
        assert delta["revision"] > first["revision"]
        runtime.sessions.clear()
        final = send(client, "Resume after losing runtime")
        rebuilt = runtime.context()
        assert runtime.sessions_created == 2
        assert rebuilt["context_delivery"] == "full"
        assert rebuilt["game"] == initial["game"]
        assert rebuilt["analysis"] == initial["analysis"]
        assert [node["san"] for node in rebuilt["variations"]] == ["e4", "d4"]
        assert rebuilt["history"] == second["messages"] + [
            {"author": "player", "body": "Resume after losing runtime"},
        ]
        assert len(final["messages"]) == 6
        assert len(final["nodes"]) == 2
        assert len(final["attempts"]) == 3
    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"),
                               trainer_runtime=runtime)) as client:
        assert client.get(URL + "/0").json() == final
        send(client, "Continue after restart")
        assert runtime.context()["context_delivery"] == "delta"
        assert runtime.context()["history"] == [{"author": "player", "body": "Continue after restart"}]


def test_long_history_is_derived_for_runtime_but_full_context_remains_readable(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    from test_trainer_mcp import call

    class ContextReadingOpenCode(RecoverableOpenCode):
        token = ""
        client: TestClient
        authoritative: dict[str, Any]
        refreshed_and_played = False

        def configure_mcp(self, url: str, token: str) -> None:
            self.token = token

        def respond(self, session_id: str, system: str, context: str) -> str:
            self.authoritative = call(self.client, self.token, "read_context", {
                "attempt_id": json.loads(context)["attempt_id"],
            })["structuredContent"]
            if "derived_summary" in json.loads(context) and not self.refreshed_and_played:
                attempt = json.loads(context)["attempt_id"]
                args = {"attempt_id": attempt, "revision": self.authoritative["revision"] - 1,
                        "active_node_id": self.authoritative["active_node_id"], "moves": ["e7e5"]}
                assert call(self.client, self.token, "play_line", args)["isError"]
                refreshed = call(self.client, self.token, "read_context", {"attempt_id": attempt})["structuredContent"]
                assert refreshed["variations"] == self.authoritative["variations"]
                args["revision"] = refreshed["revision"]
                assert not call(self.client, self.token, "play_line", args)["isError"]
                self.refreshed_and_played = True
            return super().respond(session_id, system, context)

    runtime = ContextReadingOpenCode()
    settings = migrated_settings("Lance5500")
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=runtime)) as client:
        runtime.client = client
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        messages = [(f"Plan {i}: protect the center. " + "chess " * 700).strip() for i in range(12)]
        summaries: list[dict[str, Any]] = []
        saved: dict[str, Any] = {}
        for index, body in enumerate(messages):
            saved = send(client, body, '["e2e4"]' if index == 0 else "[]")
            if "derived_summary" in runtime.context():
                summaries.append(runtime.context()["derived_summary"])
        assert summaries
        assert summaries[0]["source"] == "persisted_discussion"
        assert summaries[0]["authoritative"] is False
        assert "Plan 0: protect the center" in summaries[0]["text"]
        assert len(summaries[0]["text"]) <= 8000
        assert runtime.deleted  # Compacted runtime sessions do not accumulate.
        assert runtime.refreshed_and_played
        assert [node["san"] for node in saved["nodes"]] == ["e4", "e5"]
        assert len(saved["messages"]) == 24
        assert [m["body"] for m in saved["messages"] if m["author"] == "player"] == messages
        assert [m["body"] for m in runtime.authoritative["history"] if m["author"] == "player"] == messages
        assert runtime.authoritative["history"][0]["body"] == messages[0]
        assert runtime.authoritative["game"][1].startswith("d4 d5 c4")
        assert runtime.authoritative["analysis"]["evaluations"]
        runtime.sessions.clear()
        saved = send(client, "Recover the original plan")
        compacted = runtime.context()
        assert compacted["context_delivery"] == "full"
        assert compacted["derived_summary"]["authoritative"] is False
        assert len(compacted["history"]) < len(saved["messages"])
        assert compacted["game"] == runtime.authoritative["game"]
        assert compacted["analysis"] == runtime.authoritative["analysis"]
        assert compacted["variations"] == runtime.authoritative["variations"]
    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"))) as client:
        assert client.get(URL + "/0").json() == saved


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_discussion_deletion_cleans_session_best_effort_after_local_commit(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock, cleanup_fails: bool,
) -> None:
    runtime = RecoverableOpenCode()
    runtime.cleanup_fails = cleanup_fails
    settings = migrated_settings("Lance5500")
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        saved = send(client, "Remove this discussion", '["e2e4"]')
        for revision, confirmed in ((saved["revision"], False), (saved["revision"] - 1, True)):
            assert client.request("DELETE", URL + "/0", json={
                "revision": revision, "confirmed": confirmed,
            }).status_code in {409, 422}
            assert runtime.deleted == []
            assert client.get(URL + "/0").json() == saved
        assert client.request("DELETE", URL + "/0", json={
            "revision": saved["revision"], "confirmed": True,
        }).status_code == 204
        assert runtime.deleted == ["session-1"]
        assert client.get(URL + "/0").status_code == 404
        assert client.get("/analyses/lichess/q7ZvsdUF").status_code == 200
    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"))) as client:
        assert client.get(URL + "/0").status_code == 404


def test_unavailable_session_check_preserves_reads_and_can_recover_later(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    class UnavailableOpenCode(RecoverableOpenCode):
        unavailable = False

        def session_compatible(self, session_id: str) -> bool:
            if self.unavailable:
                raise RuntimeError("PRIVATE_SERVER_FAILURE")
            return super().session_compatible(session_id)

    runtime = UnavailableOpenCode()
    with TestClient(create_app(migrated_settings("Lance5500"),
                               lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        first = send(client, "Start")
        runtime.unavailable = True
        assert client.post(URL, data={"base_ply": "0", "message": "Keep my question"}).status_code == 503
        saved = client.get(URL + "/0").json()
        assert saved["messages"] == first["messages"] + [{"author": "player", "body": "Keep my question"}]
        assert saved["attempts"][-1]["status"] == "failed"
        assert runtime.sessions_created == 1
        assert client.get("/").status_code == 200
        assert "PRIVATE_SERVER_FAILURE" not in client.get("/analyses/lichess/q7ZvsdUF").text
        runtime.unavailable = False
        runtime.sessions.clear()
        final = send(client, "Continue")
        assert len(final["messages"]) == 5
        assert runtime.context()["history"] == saved["messages"] + [{"author": "player", "body": "Continue"}]
        assert runtime.context()["response_attempts"][1]["status"] == "failed"


@pytest.mark.parametrize("invalid", ["missing", "directory", "identity"])
def test_v1_http_session_validation_reconstructs_missing_or_incompatible_sessions(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock, invalid: str,
) -> None:
    from trainer.adapters.opencode import OpenCodeSessionClient

    requests: list[httpx.Request] = []
    prompts: list[dict[str, Any]] = []
    sessions = 0
    lose_session = False

    def server(request: httpx.Request) -> httpx.Response:
        nonlocal sessions
        requests.append(request)
        assert request.headers["Authorization"].startswith("Basic ")
        if request.url.path == "/session":
            assert request.method == "POST"
            sessions += 1
            return httpx.Response(200, json={"id": f"ses_{sessions}"})
        if request.method == "GET":
            assert request.url.path == "/session/ses_1"
            if lose_session and invalid == "missing":
                return httpx.Response(404)
            return httpx.Response(200, json={
                "id": "wrong" if lose_session and invalid == "identity" else "ses_1",
                "directory": "/other" if lose_session and invalid == "directory" else "/private/trainer",
            })
        if request.method == "DELETE":
            return httpx.Response(200, json=True)
        assert request.url.path == f"/session/ses_{sessions}/message"
        body = json.loads(request.content)
        assert body["agent"] == "trainer" and "model" not in body
        prompts.append(json.loads(body["parts"][0]["text"]))
        return httpx.Response(200, json={
            "info": {"role": "assistant", "sessionID": f"ses_{sessions}"},
            "parts": [{"type": "text", "text": "Develop a piece."}],
        })

    class HTTPRuntime(RecoverableOpenCode):
        adapter = OpenCodeSessionClient(httpx.Client(
            base_url="http://fake", auth=("ephemeral", "secret"),
            transport=httpx.MockTransport(server),
        ), directory="/private/trainer")

        def create_session(self) -> str:
            return self.adapter.create_session()

        def session_compatible(self, session_id: str) -> bool:
            return self.adapter.session_compatible(session_id)

        def delete_session(self, session_id: str) -> None:
            self.adapter.delete_session(session_id)

        def respond(self, session_id: str, system: str, context: str) -> str:
            return self.adapter.respond(session_id, system, context)

    with TestClient(create_app(migrated_settings("Lance5500"),
                               lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=HTTPRuntime())) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        send(client, "Start")
        send(client, "Continue")
        assert prompts[-1]["context_delivery"] == "delta"
        lose_session = True
        saved = send(client, "Recover")
        assert sessions == 2
        assert prompts[-1]["context_delivery"] == "full"
        assert len(prompts[-1]["history"]) == 5
        assert len(saved["messages"]) == 6
        assert client.request("DELETE", URL + "/0", json={
            "revision": saved["revision"], "confirmed": True,
        }).status_code == 204
        assert requests[-1].method == "DELETE"
        assert requests[-1].url.path == "/session/ses_2"


def test_session_rebuild_preserves_tool_evidence_and_demonstrated_moves_once(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    from test_trainer_evidence import EvidenceEvaluator
    from test_trainer_mcp import call

    class DemonstratingOpenCode(RecoverableOpenCode):
        token = ""
        client: TestClient
        demonstrated = False

        def configure_mcp(self, url: str, token: str) -> None:
            self.token = token

        def respond(self, session_id: str, system: str, context: str) -> str:
            if not self.demonstrated:
                attempt = json.loads(context)["attempt_id"]
                evidence = call(self.client, self.token, "evaluate_position", {
                    "attempt_id": attempt, "position": {"kind": "variation", "node_id": None},
                })
                assert not evidence["isError"]
                current = call(self.client, self.token, "read_context", {"attempt_id": attempt})["structuredContent"]
                assert not call(self.client, self.token, "play_line", {
                    "attempt_id": attempt, "revision": current["revision"],
                    "active_node_id": current["active_node_id"], "moves": ["e2e4", "e7e5"],
                })["isError"]
                self.demonstrated = True
            return super().respond(session_id, system, context)

    runtime = DemonstratingOpenCode()
    settings = migrated_settings("Lance5500")
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                               trainer_runtime=runtime, candidate_evaluator=EvidenceEvaluator())) as client:
        runtime.client = client
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        first = send(client, "Show a line with evidence")
        assert len(first["evidence"]) == 1
        assert len(first["nodes"]) == 2
        runtime.sessions.clear()
        saved = send(client, "Recover the evidence")
        rebuilt = runtime.context()
        assert rebuilt["context_delivery"] == "full"
        assert saved["evidence"] == first["evidence"]
        assert saved["nodes"] == first["nodes"]
        assert rebuilt["variations"][1]["san"] == "e5"
        evidence_events = [event for attempt in rebuilt["response_attempts"]
                           for event in attempt["events"] if event["type"] == "evidence"]
        assert len(evidence_events) == 1
        assert json.loads(evidence_events[0]["text"])["provenance"]["name"] == "Deterministic"
        send(client, "Continue without repeating tools")
        assert runtime.context()["variations"] == []
        assert all(event["type"] != "evidence" for attempt in runtime.context()["response_attempts"]
                   for event in attempt["events"])
