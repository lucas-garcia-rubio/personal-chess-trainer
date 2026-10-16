"""Opt-in real browser contracts; see tests/browser/README.md."""
import os
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1",
    reason="Set RUN_BROWSER_TESTS=1 to run the real desktop browser contracts",
)


@pytest.mark.parametrize("height", [600, 768, 1000])
def test_desktop_workspace(desktop_app: str, height: int, tmp_path: Path) -> None:
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("desktop.cjs")),
         desktop_app, str(height), str(tmp_path)],
        text=True, capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
