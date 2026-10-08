from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest


def _config(tmp_path: Path) -> str:
    flyway = Path(__file__).parent / "flyway_stub.py"
    flyway.chmod(0o755)
    return (
        '[lichess]\nusername = "test-player"\n\n'
        '[database]\npath = "trainer.db"\n\n'
        f'[migration]\nflyway_path = "{flyway}"\n'
    )


def test_serve_explains_how_to_create_missing_configuration(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [str(Path(sys.executable).with_name("trainer")), "serve"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Configuration file not found" in result.stderr
    assert "Create it with [lichess] username and [database] path" in result.stderr


def test_serve_starts_the_configured_application(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        _config(tmp_path),
        encoding="utf-8",
    )
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    process = subprocess.Popen(
        [
            str(Path(sys.executable).with_name("trainer")),
            "serve",
            "--port",
            str(port),
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"trainer serve exited early:\n{stdout}\n{stderr}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=0.2) as response:
                    status = response.status
                    body = response.read().decode("utf-8")
                break
            except (URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    pytest.fail("trainer serve did not become ready within 10 seconds")
                time.sleep(0.05)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    assert status == 200
    assert "Personal Chess Trainer" in body
    assert (tmp_path / "trainer.db").is_file()
    with sqlite3.connect(tmp_path / "trainer.db") as database:
        assert database.execute("PRAGMA user_version").fetchone() == (4,)


def test_serve_refuses_to_start_when_flyway_validation_fails(tmp_path: Path) -> None:
    flyway = tmp_path / "flyway"
    flyway.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = version ]; then printf 'Flyway OSS Edition 13.9.0 by Redgate\\n'; exit 0; fi\n"
        "printf 'checksum mismatch in V1' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    flyway.chmod(0o755)
    (tmp_path / "config.toml").write_text(
        '[lichess]\nusername = "test-player"\n\n'
        '[database]\npath = "trainer.db"\n\n'
        f'[migration]\nflyway_path = "{flyway}"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(Path(sys.executable).with_name("trainer")), "serve"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Flyway command 'validate' failed" in result.stderr
    assert "checksum mismatch in V1" in result.stderr
    assert not (tmp_path / "trainer.db").exists()
