"""Tail-follow contracts at the real browser and controlled runtime boundaries."""
from collections.abc import Iterator
import os
from pathlib import Path
import subprocess
import time

import pytest

from browser.conftest import LongResponseRuntime
from trainer.application.ports import ResponseControl, TrainerEvent


class GatedScrollRuntime(LongResponseRuntime):
    def __init__(self, gates: Path) -> None:
        super().__init__()
        self.gates = gates
        self.token = ""
        self.seeded = False

    def configure_mcp(self, url: str, token: str) -> None:
        self.token = token

    def stream(self, session_id: str, system: str, context: str,
               control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
        if not self.seeded:
            self.seeded = True
            yield TrainerEvent("text", self.respond(session_id, system, context))
            yield TrainerEvent("completed")
            return
        for index in range(1, 5):
            deadline = time.monotonic() + 60
            while not (self.gates / str(index)).exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("Browser did not release the next chunk")
                time.sleep(0.01)
            yield TrainerEvent("text", "\n\n" + (f"Chunk {index}: compare the open files. " * 100) + f" End chunk {index}.")
        yield TrainerEvent("completed")


@pytest.fixture
def desktop_runtime(tmp_path: Path) -> GatedScrollRuntime:
    return GatedScrollRuntime(tmp_path)


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Opt-in real desktop browser")
@pytest.mark.parametrize("return_latest", [True, False], ids=["return-latest", "keep-reading"])
@pytest.mark.parametrize("height", [600, 768])
def test_streaming_history_follows_only_at_tail(
    desktop_app: str, desktop_runtime: GatedScrollRuntime, tmp_path: Path,
    return_latest: bool, height: int,
) -> None:
    try:
        result = subprocess.run([
            "node", str(Path(__file__).with_name("scroll.cjs")), desktop_app,
            str(tmp_path), desktop_runtime.token, str(return_latest), str(height),
        ], capture_output=True, text=True, timeout=90)
    finally:
        for index in range(1, 5):
            (tmp_path / str(index)).touch()
    assert result.returncode == 0, result.stdout + result.stderr
