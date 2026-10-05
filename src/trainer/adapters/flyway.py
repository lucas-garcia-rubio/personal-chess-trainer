from pathlib import Path
import re
import shutil
import sqlite3
import subprocess

from trainer.config import MigrationSettings

_SUPPORTED_VERSION = "13.9.0"
_VERSION = re.compile(r"\bFlyway(?:\s+\w+\s+Edition)?\s+(\d+\.\d+\.\d+)")
_LEGACY_COLUMNS = [
    ("source_id", "TEXT", 0, 1),
    ("raw_document", "TEXT", 1, 0),
    ("analysis_document", "TEXT", 1, 0),
    ("created_at", "INTEGER", 1, 0),
    ("opponent", "TEXT", 1, 0),
    ("result", "TEXT", 1, 0),
    ("speed", "TEXT", 1, 0),
    ("critical_count", "INTEGER", 1, 0),
]
_LEGACY_SQL = """
CREATE TABLE games (
    source_id TEXT PRIMARY KEY,
    raw_document TEXT NOT NULL,
    analysis_document TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    opponent TEXT NOT NULL,
    result TEXT NOT NULL,
    speed TEXT NOT NULL,
    critical_count INTEGER NOT NULL
)
"""


class FlywayError(RuntimeError):
    """Raised when startup cannot establish a validated database schema."""


class FlywayMigrator:
    def __init__(self, settings: MigrationSettings) -> None:
        self._settings = settings

    def migrate(self, database_path: Path) -> None:
        database_path = database_path.expanduser().resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        state = _database_state(database_path)
        if state == "unknown":
            raise FlywayError(
                f"SQLite database at {database_path} is not the recognized legacy schema. "
                "Restore a valid backup or configure a different database path."
            )

        executable = self._executable()
        version = self._run([executable, "version"])
        match = _VERSION.search(version)
        if match is None or match.group(1) != _SUPPORTED_VERSION:
            reported = version.strip() or "unknown version"
            raise FlywayError(
                f"Flyway {_SUPPORTED_VERSION} is required; executable {executable} reported "
                f"{reported!r}. Install a supported Flyway release or set "
                "[migration] flyway_path."
            )

        common = [
            executable,
            f"-url=jdbc:sqlite:{database_path}",
            f"-locations=filesystem:{_migration_directory()}",
        ]
        if state == "legacy":
            self._run([*common, "-baselineVersion=1", "baseline"])
        self._run([*common, "-ignoreMigrationPatterns=*:pending", "validate"])
        self._run([*common, "migrate"])

    def _executable(self) -> str:
        configured = self._settings.executable
        if configured is not None:
            return str(configured)
        executable = shutil.which("flyway")
        if executable is None:
            raise FlywayError(
                f"No Flyway executable found on PATH. Install Flyway {_SUPPORTED_VERSION} or set "
                "[migration] flyway_path."
            )
        return executable

    @staticmethod
    def _run(command: list[str]) -> str:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise FlywayError(
                f"Could not start Flyway executable {command[0]}: {error}."
            ) from error
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no error output"
            raise FlywayError(
                f"Flyway command {command[-1]!r} failed: {detail}. "
                "Fix the migration error before starting the server."
            )
        return result.stdout + result.stderr


def _migration_directory() -> Path:
    return Path(__file__).resolve().parents[1] / "migrations"


def _database_state(path: Path) -> str:
    if not path.exists() or path.stat().st_size == 0:
        return "empty"
    try:
        with sqlite3.connect(path) as database:
            tables = {
                str(row[0])
                for row in database.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if not tables:
                return "empty"
            if "flyway_schema_history" in tables:
                return "managed"
            if tables != {"games"}:
                return "unknown"
            columns = [
                (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
                for row in database.execute("PRAGMA table_info(games)")
            ]
            schema_row = database.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'games'"
            ).fetchone()
            schema_sql = "" if schema_row is None or schema_row[0] is None else str(schema_row[0])
            extra_indexes = any(
                str(row[3]) != "pk"
                for row in database.execute("PRAGMA index_list(games)")
            )
            user_version = int(database.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error:
        return "unknown"
    exact_legacy = (
        columns == _LEGACY_COLUMNS
        and _normalize_schema(schema_sql) == _normalize_schema(_LEGACY_SQL)
        and not extra_indexes
        and user_version == 2
    )
    return "legacy" if exact_legacy else "unknown"


def _normalize_schema(sql: str) -> str:
    return " ".join(sql.casefold().split())
