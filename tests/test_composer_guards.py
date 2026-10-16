"""Public HTTP protections shared by keyboard, button and direct clients."""
from collections.abc import Callable, Iterator
from threading import Event

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from test_trainer_response import FakeOpenCode
from test_trainer_stream import URL, events
from trainer.application.ports import ResponseControl, TrainerEvent
from trainer.bootstrap import create_app
from trainer.config import Settings


def test_pending_send_and_revoked_consent_cannot_create_requests_or_retries(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    ready = Event()
    release = Event()

    class GatedRuntime(FakeOpenCode):
        def stream(self, session_id: str, system: str, context: str,
                   control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
            yield TrainerEvent("text", "Saved partial explanation.")
            ready.set()
            assert release.wait(10)
            yield TrainerEvent("completed")

        def abort(self, session_id: str) -> None:
            release.set()

    with TestClient(create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500"), trainer_runtime=GatedRuntime(),
    )) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Explain", "stream": "true"}).json()
        assert ready.wait(5)
        try:
            before = client.get(URL + "/0").json()
            for streaming in ("true", "false"):
                assert client.post(URL, data={
                    "base_ply": "0", "message": "Concurrent draft", "stream": streaming,
                }).status_code == 409
            assert client.get(URL + "/0").json() == before
            attempt_url = URL + f'/0/attempts/{attempt["id"]}'
            assert client.post(attempt_url + "/retry").status_code == 422
            assert client.get(URL + "/0").json() == before
            assert client.post(attempt_url + "/interrupt").status_code == 200
            assert events(client, attempt["eventsUrl"])[-1]["type"] == "interrupted"
            stopped = client.get(URL + "/0").json()
            assert client.post("/trainer/consent", json={"accepted": False}).json() == {"accepted": False}
            for streaming in ("true", "false"):
                assert client.post(URL, data={
                    "base_ply": "0", "message": "Unconsented draft", "stream": streaming,
                }).status_code == 403
            assert client.post(attempt_url + "/retry").status_code == 403
            assert client.get(URL + "/0").json() == stopped
            client.post("/trainer/consent", json={"accepted": True})
            assert client.get(URL + "/0").json() == stopped
            explicit = client.post(URL, data={"base_ply": "0", "message": "Explicit draft", "stream": "true"})
            assert explicit.status_code == 202
            assert events(client, explicit.json()["eventsUrl"])[-1]["type"] == "completed"
            final = client.get(URL + "/0").json()
            assert [m["body"] for m in final["messages"] if m["author"] == "player"] == ["Explain", "Explicit draft"]
        finally:
            release.set()
