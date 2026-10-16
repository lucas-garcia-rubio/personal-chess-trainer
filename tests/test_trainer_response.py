from collections.abc import Callable, Iterator
import json
from typing import cast

import httpx

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from test_discussion import _trainer_data
from test_trainer_runtime import FakeTrainerRuntime
from trainer.application.ports import ResponseControl, TrainerEvent, TrainerRuntimeAvailability
from trainer.bootstrap import create_app
from trainer.config import Settings


class FakeOpenCode:
    def stream(self, session_id: str, system: str, context: str,
               control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
        yield TrainerEvent("text", self.respond(session_id, system, context))
        yield TrainerEvent("completed")

    def abort(self, session_id: str) -> None:
        pass

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sessions_created = 0

    def start(self) -> TrainerRuntimeAvailability:
        return TrainerRuntimeAvailability.available()

    def stop(self) -> None:
        pass

    def create_session(self) -> str:
        self.sessions_created += 1
        return "session-first"

    def session_compatible(self, session_id: str) -> bool:
        return True

    def delete_session(self, session_id: str) -> None:
        pass

    def respond(self, session_id: str, system: str, context: str) -> str:
        self.requests.append(httpx.Request("POST", f"http://fake/{session_id}", json={
            "system": system, "context": json.loads(context),
        }))
        return '<script>alert("hostile")</script><img src=x onerror=alert(1)>Consider Nf3 & development.'


def test_trainer_has_no_consent_settings_and_sends_without_opt_in(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock
) -> None:
    settings = migrated_settings("Lance5500")
    runtime = FakeOpenCode()
    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.games_for("Lance5500"),
        trainer_runtime=runtime,
    )) as client:
        client.post("/sync")
        page = client.get("/analyses/lichess/q7ZvsdUF")
        assert "Trainer settings and consent" not in page.text
        assert "trainer-consent" not in page.text
        assert client.get("/trainer/consent").json() == {"accepted": False}
        assert client.post("/analyses/lichess/q7ZvsdUF/discussions", data={
            "base_ply": "0", "message": "Why?", "initial_moves": "[]",
        }).status_code == 200

    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.fail_on_request("offline reopen"),
        trainer_runtime=runtime,
    )) as client:
        assert client.get("/trainer/consent").json() == {"accepted": False}
        assert client.post("/analyses/lichess/q7ZvsdUF/discussions", data={
            "base_ply": "0", "message": "Why?", "initial_moves": "[]",
        }).status_code == 200


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
        assert runtime.requests == []
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
        assert runtime.sessions_created == 1
        assert runtime.requests[1].url.path == "/session-first"


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
        assert requests == []
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


def test_context_tracks_critical_moment_all_branches_and_selected_position(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock
) -> None:
    document = json.loads(lichess_mock.fixture)
    document["moves"] = "d4 d5"
    document["analysis"] = [{"eval": -400, "best": "e2e4"}, {"eval": -400}]
    runtime = FakeOpenCode()
    with TestClient(create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500", ndjson=json.dumps(document)),
        trainer_runtime=runtime,
    )) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        page = client.post("/analyses/lichess/q7ZvsdUF/discussions", data={
            "base_ply": "0", "message": "Compare", "initial_moves": '["e2e4"]',
        })
        discussion = cast(dict[str, dict[str, object]], _trainer_data(page.text)["discussions"])["0"]
        branch = client.post("/analyses/lichess/q7ZvsdUF/discussions/0/moves", json={
            "revision": discussion["revision"], "parentId": None, "uci": "d2d4",
        })
        assert branch.status_code == 200
        client.post("/analyses/lichess/q7ZvsdUF/discussions", data={
            "base_ply": "0", "message": "Compare both", "initial_moves": "[]",
        })
        context = json.loads(runtime.requests[-1].content)["context"]
        initial = json.loads(runtime.requests[0].content)["context"]
        assert initial["critical_moment"]["ply"] == 1
        assert initial["critical_moment"]["played"] == "d4"
        assert initial["critical_moment"]["best"] == "e4"
        assert initial["critical_moment"]["classification"] == "Blunder"
        assert [node["san"] for node in initial["variations"]] == ["e4"]
        assert [node["san"] for node in context["variations"]] == ["d4"]
        assert context["active_position"] == branch.json()["activeFen"]
        assert context["active_node_id"] == branch.json()["activeNodeId"]
        assert context["revision"] > branch.json()["revision"]
        assert context["history"][-1] == {"author": "player", "body": "Compare both"}


def test_provider_failure_keeps_player_message_without_leaking_technical_details(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock
) -> None:
    class FailingOpenCode(FakeOpenCode):
        def respond(self, session_id: str, system: str, context: str) -> str:
            raise RuntimeError("PRIVATE_PROVIDER_ERROR ephemeral-password HIDDEN_REASONING")

    settings = migrated_settings("Lance5500")
    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.games_for("Lance5500"),
        trainer_runtime=FailingOpenCode(),
    )) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        response = client.post("/analyses/lichess/q7ZvsdUF/discussions", data={
            "base_ply": "0", "message": "Help me calculate", "initial_moves": "[]",
        })
        assert response.status_code == 503
        assert response.json() == {"detail": "Trainer response failed. Your Discussion is saved; please try again."}
        assert client.get("/").status_code == 200

    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.fail_on_request("offline reopen"),
    )) as client:
        page = client.get("/analyses/lichess/q7ZvsdUF")
        discussion = cast(dict[str, dict[str, object]], _trainer_data(page.text)["discussions"])["0"]
        assert discussion["messages"] == [{"author": "player", "body": "Help me calculate"}]
        for excluded in ("PRIVATE_PROVIDER_ERROR", "ephemeral-password", "HIDDEN_REASONING"):
            assert excluded not in page.text
