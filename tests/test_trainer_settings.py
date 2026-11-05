from collections.abc import Callable
from dataclasses import replace

from fastapi.testclient import TestClient

from trainer.bootstrap import create_app
from trainer.config import OpenCodeSettings, Settings
import pytest


class Models:
    def list_models(self) -> list[str]:
        return ["openai/gpt-6.1-sol", "other/trainer"]


def test_home_links_to_settings_and_lists_opencode_models(
    migrated_settings: Callable[[str], Settings],
) -> None:
    settings = replace(migrated_settings("Player"),
                       opencode=OpenCodeSettings(model="openai/gpt-6.1-sol"))
    with TestClient(create_app(settings, model_catalog=Models())) as client:
        assert 'href="/settings"' in client.get("/").text
        page = client.get("/settings")
        assert page.status_code == 200
        assert '<label for="trainer-model">Trainer model</label>' in page.text
        assert 'value="openai/gpt-6.1-sol" selected' in page.text
        assert 'value="other/trainer"' in page.text


def test_saved_model_survives_restart_and_overrides_config_default(
    migrated_settings: Callable[[str], Settings],
) -> None:
    settings = replace(migrated_settings("Player"),
                       opencode=OpenCodeSettings(model="openai/gpt-6.1-sol"))
    with TestClient(create_app(settings, model_catalog=Models())) as client:
        saved = client.post("/settings", data={"model": "other/trainer"}, follow_redirects=False)
        assert saved.status_code == 303
        assert saved.headers["location"] == "/settings"
    with TestClient(create_app(settings, model_catalog=Models())) as client:
        assert 'value="other/trainer" selected' in client.get("/settings").text


@pytest.mark.parametrize("model", ["unknown/model", "", "openai/gpt-6.1-sol; echo nope"])
def test_invalid_model_does_not_replace_saved_selection(
    migrated_settings: Callable[[str], Settings], model: str,
) -> None:
    with TestClient(create_app(migrated_settings("Player"), model_catalog=Models())) as client:
        assert client.post("/settings", data={"model": "other/trainer"}).status_code == 200
        response = client.post("/settings", data={"model": model})
        assert response.status_code == 422
        assert "Choose a model available through OpenCode." in response.text
        assert 'value="other/trainer" selected' in client.get("/settings").text


@pytest.mark.parametrize("failed", [False, True])
def test_unavailable_catalog_shows_recoverable_settings_without_changing_model(
    migrated_settings: Callable[[str], Settings], failed: bool,
) -> None:
    class OfflineModels:
        offline = False

        def list_models(self) -> list[str]:
            if self.offline:
                if failed:
                    raise RuntimeError("Could not list OpenCode models.")
                return []
            return ["other/trainer"]

    models = OfflineModels()
    with TestClient(create_app(migrated_settings("Player"), model_catalog=models)) as client:
        client.post("/settings", data={"model": "other/trainer"})
        models.offline = True
        response = client.get("/settings")
        assert response.status_code == 503
        assert 'role="alert"' in response.text
        assert '<button type="submit" disabled>' in response.text
        assert "other/trainer" in response.text
        assert client.get("/").status_code == 200
        assert client.post("/settings", data={"model": "new/model"}).status_code == 503
        models.offline = False
        assert 'value="other/trainer" selected' in client.get("/settings").text
