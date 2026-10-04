from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from trainer.bootstrap import create_app
from trainer.config import Settings


def test_home_is_served_with_a_real_local_database(
    tmp_path: Path,
    lichess_mock: LichessMock,
) -> None:
    database_path = tmp_path / "trainer.db"

    app = create_app(
        Settings(lichess_username="test-player", database_path=database_path),
        lichess_transport=lichess_mock.fail_on_request(
            "rendering Home must not call Lichess"
        ),
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Personal Chess Trainer" in response.text
    assert database_path.is_file()
    with sqlite3.connect(database_path) as database:
        assert database.execute("PRAGMA user_version").fetchone() == (2,)
