"""Mutually exclusive category/exact modes through Home's browser boundary."""
import os
from pathlib import Path
import subprocess

import pytest

from test_home import category_games_settings as category_games_settings
from trainer.config import Settings


@pytest.fixture
def result_games_settings(category_games_settings: Settings) -> Settings:
    return category_games_settings


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Opt-in real desktop browser")
def test_home_time_modes(home_app: str) -> None:
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("home_time_modes.cjs")), home_app],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
