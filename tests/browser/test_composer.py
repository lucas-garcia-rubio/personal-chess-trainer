"""Composer contracts against a real server and gated Trainer stream."""
from collections.abc import Iterator
import os
from pathlib import Path
import subprocess
import time

import pytest

from test_trainer_response import FakeOpenCode
from trainer.application.ports import ResponseControl, TrainerEvent


class ComposerRuntime(FakeOpenCode):
    def __init__(self, gates: Path) -> None:
        super().__init__()
        self.gates = gates
        self.calls = 0

    def stream(self, session_id: str, system: str, context: str,
               control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
        self.calls += 1
        if self.calls == 1:  # The shared harness seeds an existing Discussion.
            yield TrainerEvent("text", "Existing explanation.")
        else:
            yield TrainerEvent("text", "First live paragraph.")
            for stage in (1, 2):
                deadline = time.monotonic() + 30
                while not (self.gates / str(stage)).exists():
                    if control and control.cancelled.is_set():
                        return
                    assert time.monotonic() < deadline, "Browser did not release stream gate"
                    time.sleep(0.01)
                yield TrainerEvent("text", f" Live paragraph {stage}.")
        yield TrainerEvent("completed")


@pytest.fixture
def desktop_runtime(tmp_path: Path) -> FakeOpenCode:
    return ComposerRuntime(tmp_path)


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Opt-in real desktop browser")
@pytest.mark.parametrize("ending", ["complete", "interrupt"])
def test_composer_during_gated_stream(desktop_app: str, tmp_path: Path, ending: str) -> None:
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("composer.cjs")), desktop_app, str(tmp_path), ending],
        text=True, capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
