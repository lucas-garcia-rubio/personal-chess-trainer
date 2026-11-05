"""Real browser contract for immediate Home filtering and navigation context."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Opt-in real desktop browser")
def test_home_result_filters(home_app: str) -> None:
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("home_filters.cjs")), home_app],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
