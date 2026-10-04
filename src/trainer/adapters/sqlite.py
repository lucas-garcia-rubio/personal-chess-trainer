import sqlite3
from pathlib import Path


class SQLiteStorage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA user_version = 1")
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()
