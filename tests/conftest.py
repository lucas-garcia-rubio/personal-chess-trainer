from collections.abc import Callable
from pathlib import Path
import sqlite3

import pytest

from lichess_mock import LichessMock, load_lichess_fixture
from trainer.config import Settings


@pytest.fixture
def lichess_mock() -> LichessMock:
    return LichessMock(load_lichess_fixture())


@pytest.fixture
def migrated_settings(tmp_path: Path) -> Callable[[str], Settings]:
    def create(username: str) -> Settings:
        database_path = tmp_path / "trainer.db"
        if not database_path.exists():
            migration_directory = (
                Path(__file__).parents[1] / "src" / "trainer" / "migrations"
            )
            with sqlite3.connect(database_path) as database:
                for migration in sorted(migration_directory.glob("V*__*.sql")):
                    database.executescript(migration.read_text(encoding="utf-8"))
        return Settings(lichess_username=username, database_path=database_path)

    return create
