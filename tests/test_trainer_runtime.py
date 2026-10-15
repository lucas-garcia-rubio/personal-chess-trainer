from collections.abc import Callable, Iterator
from dataclasses import dataclass
from io import StringIO
import logging

import pytest

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from trainer.application.ports import ResponseControl, TrainerEvent, TrainerRuntimeAvailability
from trainer.bootstrap import create_app
from trainer.config import Settings
from trainer.adapters.opencode import OpenCodeRuntime


@dataclass
class FakeTrainerRuntime:
    def stream(self, session_id: str, system: str, context: str,
               control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
        raise AssertionError("Availability checks must not contact a model")

    def abort(self, session_id: str) -> None:
        raise AssertionError("Availability checks must not contact a model")

    availability: TrainerRuntimeAvailability
    starts: int = 0
    stops: int = 0

    def start(self) -> TrainerRuntimeAvailability:
        self.starts += 1
        return self.availability

    def stop(self) -> None:
        self.stops += 1

    def create_session(self) -> str:
        raise AssertionError("Availability checks must not create a model session")

    def session_compatible(self, session_id: str) -> bool:
        raise AssertionError("Availability checks must not inspect model sessions")

    def delete_session(self, session_id: str) -> None:
        raise AssertionError("Availability checks must not delete model sessions")

    def respond(self, session_id: str, system: str, context: str) -> str:
        raise AssertionError("Availability checks must not contact a model")


def test_production_startup_preserves_server_readiness_logs(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unavailable_runtime(self: OpenCodeRuntime) -> TrainerRuntimeAvailability:
        return TrainerRuntimeAvailability.unavailable("Offline test runtime")

    monkeypatch.setattr(OpenCodeRuntime, "start", unavailable_runtime)
    output = StringIO()
    logger = logging.getLogger("uvicorn.error")
    monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(output)])
    monkeypatch.setattr(logger, "propagate", False)
    settings = migrated_settings("Lance5500")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request("startup is offline"),
        trainer_runtime=OpenCodeRuntime(settings.opencode),
    )
    with caplog.at_level(logging.INFO, logger="uvicorn.error"), TestClient(app) as client:
        assert client.get("/").status_code == 200
        # The host server emits this after lifespan startup finishes.
        logger.info("Application startup complete.")
        assert "Application startup complete." in output.getvalue()


def test_analysis_shows_available_trainer_runtime(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    runtime = FakeTrainerRuntime(TrainerRuntimeAvailability.available())
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500"),
        trainer_runtime=runtime,
    )

    with TestClient(app) as client:
        client.post("/sync")
        response = client.get("/analyses/lichess/q7ZvsdUF")

    assert response.status_code == 200
    assert "Trainer available" in response.text
    assert "Send to Trainer" in response.text
    assert runtime.starts == 1
    assert runtime.stops == 1


def test_unavailable_trainer_keeps_the_application_available(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    runtime = FakeTrainerRuntime(
        TrainerRuntimeAvailability.unavailable(
            "OpenCode executable was not found. Configure [opencode] path or add opencode to PATH."
        )
    )
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500"),
        trainer_runtime=runtime,
    )

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/imports/new").status_code == 200
        assert client.post("/sync").status_code == 200
        response = client.get("/analyses/lichess/q7ZvsdUF")

    assert response.status_code == 200
    assert "Trainer unavailable" in response.text
    assert "OpenCode executable was not found" in response.text
    assert '<button type="submit" disabled>Send to Trainer</button>' in response.text
    assert runtime.starts == 1
    assert runtime.stops == 1
