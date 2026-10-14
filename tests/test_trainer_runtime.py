from collections.abc import Callable
from dataclasses import dataclass

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from trainer.application.ports import TrainerRuntimeAvailability
from trainer.bootstrap import create_app
from trainer.config import Settings


@dataclass
class FakeTrainerRuntime:
    availability: TrainerRuntimeAvailability
    starts: int = 0
    stops: int = 0

    def start(self) -> TrainerRuntimeAvailability:
        self.starts += 1
        return self.availability

    def stop(self) -> None:
        self.stops += 1


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
