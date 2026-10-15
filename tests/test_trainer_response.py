from collections.abc import Callable
import json

import httpx

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from test_trainer_runtime import FakeTrainerRuntime
from trainer.application.ports import TrainerRuntimeAvailability
from trainer.bootstrap import create_app
from trainer.config import Settings
from test_discussion import _trainer_data
from typing import cast


class FakeOpenCode:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def start(self) -> TrainerRuntimeAvailability:
        return TrainerRuntimeAvailability.available()

    def stop(self) -> None:
        pass

    def create_session(self) -> str:
        return "session-first"

    def respond(self, session_id: str, system: str, context: str) -> str:
        self.requests.append(httpx.Request("POST", f"http://fake/{session_id}", json={
            "system": system, "context": json.loads(context),
        }))
        return '<script>alert("hostile")</script><img src=x onerror=alert(1)>Consider Nf3 & development.'


def test_consent_is_informed_persistent_and_revocable(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock
) -> None:
    settings = migrated_settings("Lance5500")
    runtime = FakeTrainerRuntime(TrainerRuntimeAvailability.available())
    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.games_for("Lance5500"),
        trainer_runtime=runtime,
    )) as client:
        client.post("/sync")
        page = client.get("/analyses/lichess/q7ZvsdUF")
        assert "configured provider" in page.text
        assert "Game moves, Analysis, Variations and Discussion text" in page.text
        assert client.get("/trainer/consent").json() == {"accepted": False}
        assert client.post("/analyses/lichess/q7ZvsdUF/discussions", data={
            "base_ply": "0", "message": "Why?", "initial_moves": "[]",
        }).status_code == 403
        assert client.post("/trainer/consent", json={"accepted": True}).status_code == 200

    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.fail_on_request("offline reopen"),
        trainer_runtime=runtime,
    )) as client:
        assert client.get("/trainer/consent").json() == {"accepted": True}
        assert client.post("/trainer/consent", json={"accepted": False}).json() == {"accepted": False}
        assert client.post("/analyses/lichess/q7ZvsdUF/discussions", data={
            "base_ply": "0", "message": "Why?", "initial_moves": "[]",
        }).status_code == 403


def test_first_response_has_full_context_and_survives_restart_safely(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock
) -> None:
    settings = migrated_settings("Lance5500")
    runtime = FakeOpenCode()
    fixture = json.loads(lichess_mock.fixture)
    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.games_for("Lance5500"),
        trainer_runtime=runtime,
    )) as client:
        client.post("/sync")
        fields = {"base_ply": "0", "message": "Explique diretamente", "initial_moves": '["e2e4"]'}
        assert client.post("/analyses/lichess/q7ZvsdUF/discussions", data=fields).status_code == 403
        assert runtime.requests == []
        client.post("/trainer/consent", json={"accepted": True})
        response = client.post("/analyses/lichess/q7ZvsdUF/discussions", data=fields)
        assert response.status_code == 200
        data = cast(dict[str, dict[str, object]], _trainer_data(response.text)["discussions"])["0"]
        assert data["messages"] == [
            {"author": "player", "body": "Explique diretamente"},
            {"author": "trainer", "body": 'Consider Nf3 & development.'},
        ]
        assert len(runtime.requests) == 1
        prompt = json.loads(runtime.requests[0].content)
        context = prompt["context"]
        assert context["game"][1] == fixture["moves"]
        assert context["analysis"]["evaluations"]
        assert context["player_rating"] == fixture["players"][context["analysis"]["player_color"]]["rating"]
        assert context["base_ply"] == 0
        assert context["active_position"].startswith("rnbqkbnr/pppppppp/8/8/4P3")
        assert context["variations"][0]["uci"] == "e2e4"
        assert context["history"] == [{"author": "player", "body": "Explique diretamente"}]
        for instruction in ("Socratic", "direct answer", "Player's language", "SAN", "unrelated", "later Game moves"):
            assert instruction in prompt["system"]

    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.fail_on_request("offline reopen"),
        trainer_runtime=runtime,
    )) as client:
        page = client.get("/analyses/lichess/q7ZvsdUF")
        assert _trainer_data(page.text)["discussions"] == {"0": data}
        client.post("/analyses/lichess/q7ZvsdUF/discussions", data={
            "base_ply": "0", "message": "And now?", "initial_moves": "[]",
        })
        assert len(runtime.requests) == 2
        assert runtime.requests[1].url.path == "/session-first"
        client.post("/trainer/consent", json={"accepted": False})
        assert client.post("/analyses/lichess/q7ZvsdUF/discussions", data=fields).status_code == 403
        assert len(runtime.requests) == 2


def test_production_http_exchange_only_persists_visible_assistant_text(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock
) -> None:
    from trainer.adapters.opencode import OpenCodeSessionClient

    requests: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"].startswith("Basic ")
        if request.url.path == "/session":
            assert request.method == "POST"
            return httpx.Response(200, json={"id": "ses_private"})
        assert request.url.path == "/session/ses_private/message"
        body = json.loads(request.content)
        assert body["agent"] == "trainer"
        assert "model" not in body
        assert body["parts"][0]["type"] == "text"
        assert json.loads(body["parts"][0]["text"])["history"][0]["body"] == "What now?"
        return httpx.Response(200, json={
            "info": {"role": "assistant", "sessionID": "ses_private"},
            "parts": [
                {"type": "reasoning", "text": "HIDDEN_REASONING"},
                {"type": "tool", "state": {"output": "RAW_TOOL_EVENT"}},
                {"type": "text", "text": "TECHNICAL_PROMPT", "synthetic": True},
                {"type": "text", "text": "IGNORED_TEXT", "ignored": True},
                {"type": "text", "text": "WRONG_SESSION", "sessionID": "other"},
                {"type": "text", "text": '<script>EXECUTABLE</script><b>Nf3</b> develops a piece.'},
            ],
        })

    class HTTPRuntime(FakeOpenCode):
        def __init__(self) -> None:
            super().__init__()
            self.adapter = OpenCodeSessionClient(httpx.Client(
                base_url="http://fake", auth=("ephemeral-user", "ephemeral-password"),
                transport=httpx.MockTransport(server),
            ))

        def create_session(self) -> str:
            return self.adapter.create_session()

        def respond(self, session_id: str, system: str, context: str) -> str:
            return self.adapter.respond(session_id, system, context)

    settings = migrated_settings("Lance5500")
    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.games_for("Lance5500"),
        trainer_runtime=HTTPRuntime(),
    )) as client:
        client.post("/sync")
        fields = {"base_ply": "0", "message": "What now?", "initial_moves": "[]"}
        assert client.post("/analyses/lichess/q7ZvsdUF/discussions", data=fields).status_code == 403
        assert requests == []
        client.post("/trainer/consent", json={"accepted": True})
        page = client.post("/analyses/lichess/q7ZvsdUF/discussions", data=fields)
        assert page.status_code == 200
        assert len(requests) == 2
        discussion = cast(dict[str, dict[str, object]], _trainer_data(page.text)["discussions"])["0"]
        assert discussion["messages"] == [
            {"author": "player", "body": "What now?"},
            {"author": "trainer", "body": "Nf3 develops a piece."},
        ]

    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.fail_on_request("offline reopen"),
    )) as client:
        page = client.get("/analyses/lichess/q7ZvsdUF")
        assert _trainer_data(page.text)["discussions"] == {"0": discussion}
        for excluded in ("HIDDEN_REASONING", "RAW_TOOL_EVENT", "TECHNICAL_PROMPT", "IGNORED_TEXT", "WRONG_SESSION", "EXECUTABLE", "ephemeral-user", "ephemeral-password"):
            assert excluded not in page.text
