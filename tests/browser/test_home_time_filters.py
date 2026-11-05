"""Exact time controls combined with results through the existing Home server."""
import os
from pathlib import Path
import subprocess

import pytest

from trainer.config import Settings


@pytest.fixture
def result_games_settings(time_games_settings: Settings) -> Settings:
    return time_games_settings


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Opt-in real desktop browser")
def test_home_exact_time_controls(home_app: str) -> None:
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("home_time_filters.cjs")), home_app],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
