import json
import os
import sqlite3
from pathlib import Path

import pytest

from trainer.adapters.flyway import FlywayError, FlywayMigrator
from trainer.adapters.sqlite import SQLiteStorage
from trainer.config import MigrationSettings


def _legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as database:
        database.executescript(
            """
            CREATE TABLE games (
                source_id TEXT PRIMARY KEY,
                raw_document TEXT NOT NULL,
                analysis_document TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                opponent TEXT NOT NULL,
                result TEXT NOT NULL,
                speed TEXT NOT NULL,
                critical_count INTEGER NOT NULL
            );
            PRAGMA user_version = 2;
            """
        )


def _fake_flyway(tmp_path: Path, version: str = "13.9.0") -> tuple[Path, Path]:
    log_path = tmp_path / "flyway.log"
    executable = tmp_path / "flyway"
    executable.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{log_path}'\n"
        f"if [ \"$1\" = version ]; then printf 'Flyway OSS Edition {version} by Redgate\\n'; fi\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log_path


def test_empty_database_is_validated_then_migrated(tmp_path: Path) -> None:
    executable, log_path = _fake_flyway(tmp_path)
    database_path = tmp_path / "data" / "trainer.db"

    FlywayMigrator(MigrationSettings(executable=executable)).migrate(database_path)

    commands = log_path.read_text(encoding="utf-8").splitlines()
    assert commands[0] == "version"
    assert commands[1].endswith(" validate")
    assert commands[2].endswith(" migrate")
    assert all(
        f"-url=jdbc:sqlite:{database_path.resolve()}" in line for line in commands[1:]
    )


def test_exact_legacy_database_is_baselined_before_validation_and_migration(
    tmp_path: Path,
) -> None:
    executable, log_path = _fake_flyway(tmp_path)
    database_path = tmp_path / "trainer.db"
    _legacy_database(database_path)

    FlywayMigrator(MigrationSettings(executable=executable)).migrate(database_path)

    commands = log_path.read_text(encoding="utf-8").splitlines()
    assert commands[1].endswith(" -baselineVersion=1 baseline")
    assert commands[2].endswith(" validate")
    assert commands[3].endswith(" migrate")


def test_unknown_nonempty_database_is_refused_without_running_flyway(
    tmp_path: Path,
) -> None:
    executable, log_path = _fake_flyway(tmp_path)
    database_path = tmp_path / "trainer.db"
    with sqlite3.connect(database_path) as database:
        database.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

    with pytest.raises(FlywayError, match="not the recognized legacy schema"):
        FlywayMigrator(MigrationSettings(executable=executable)).migrate(database_path)

    assert not log_path.exists()


def test_legacy_recognition_rejects_a_near_match_with_different_constraints(
    tmp_path: Path,
) -> None:
    executable, log_path = _fake_flyway(tmp_path)
    database_path = tmp_path / "trainer.db"
    with sqlite3.connect(database_path) as database:
        database.executescript(
            """
            CREATE TABLE games (
                source_id TEXT PRIMARY KEY,
                raw_document TEXT NOT NULL,
                analysis_document TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                opponent TEXT NOT NULL,
                result TEXT NOT NULL,
                speed TEXT NOT NULL,
                critical_count INTEGER NOT NULL CHECK (critical_count >= 0)
            );
            PRAGMA user_version = 2;
            """
        )

    with pytest.raises(FlywayError, match="not the recognized legacy schema"):
        FlywayMigrator(MigrationSettings(executable=executable)).migrate(database_path)

    assert not log_path.exists()


def test_incompatible_flyway_version_is_actionable(tmp_path: Path) -> None:
    executable, _ = _fake_flyway(tmp_path, version="13.8.0")

    with pytest.raises(FlywayError, match="Flyway 13.9.0 is required"):
        FlywayMigrator(MigrationSettings(executable=executable)).migrate(
            tmp_path / "trainer.db"
        )


def test_flyway_is_resolved_from_path_when_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    executable, log_path = _fake_flyway(bin_directory)
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")

    FlywayMigrator(MigrationSettings()).migrate(tmp_path / "trainer.db")

    assert executable.name == "flyway"
    assert log_path.read_text(encoding="utf-8").splitlines()[0] == "version"


def test_validation_failure_stops_before_migration_with_actionable_output(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "flyway"
    executable.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = version ]; then printf 'Flyway OSS Edition 13.9.0 by Redgate\\n'; exit 0; fi\n"
        "for last do :; done\n"
        "if [ \"$last\" = validate ]; then printf 'checksum mismatch in V1' >&2; exit 1; fi\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    with pytest.raises(FlywayError) as error:
        FlywayMigrator(MigrationSettings(executable=executable)).migrate(
            tmp_path / "trainer.db"
        )

    assert "validate" in str(error.value)
    assert "checksum mismatch in V1" in str(error.value)
    assert "Fix the migration error" in str(error.value)


def test_legacy_migration_backfills_metadata_without_changing_raw_or_analysis_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trainer.db"
    _legacy_database(database_path)
    raw_document = (
        (Path(__file__).parent / "fixtures" / "lichess_game.ndjson")
        .read_text(encoding="utf-8")
        .strip()
    )
    legacy_analysis = {
        "source_id": "q7ZvsdUF",
        "created_at": 1514505150384,
        "opponent": "TryingHard87",
        "result": "draw",
        "speed": "blitz",
        "time_control": "300+3",
        "player_color": "white",
        "critical_moments": [],
    }
    with sqlite3.connect(database_path) as database:
        database.execute(
            "INSERT INTO games VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "q7ZvsdUF",
                raw_document,
                json.dumps(legacy_analysis),
                1514505150384,
                "TryingHard87",
                "draw",
                "blitz",
                0,
            ),
        )

    flyway = Path(__file__).parent / "flyway_stub.py"
    flyway.chmod(0o755)
    FlywayMigrator(MigrationSettings(executable=flyway)).migrate(database_path)

    with sqlite3.connect(database_path) as database:
        stored = database.execute(
            """
            SELECT raw_document, analysis_document, origin, origin_id, played_at,
                    white, black, game_result, time_control, eco, opening,
                    headers_document, canonical_document
            FROM games
            """
        ).fetchone()
    assert stored is not None
    assert stored[0] == raw_document
    migrated_analysis = json.loads(stored[1])
    assert {key: migrated_analysis[key] for key in legacy_analysis} == legacy_analysis
    assert migrated_analysis["evaluator"] == {
        "source_kind": "lichess-server",
        "name": "Lichess",
        "version": None,
        "parameters": {},
    }
    assert stored[2:11] == (
        "lichess",
        "q7ZvsdUF",
        1514505150384,
        "Lance5500",
        "TryingHard87",
        "1/2-1/2",
        "300+3",
        "D31",
        "Semi-Slav Defense: Marshall Gambit",
    )
    headers = json.loads(stored[11])
    assert headers["Event"] == "2017 Winter Marathon"
    assert headers["WhiteElo"] == "2389"
    assert headers["BlackElo"] == "2498"
    assert headers["WhiteRatingDiff"] == "+4"
    assert headers["BlackRatingDiff"] == "-4"
    assert json.loads(stored[12]) == [
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        json.loads(raw_document)["moves"],
        "Lance5500",
        "TryingHard87",
        "1/2-1/2",
    ]

    storage = SQLiteStorage(database_path)
    try:
        reopened = storage.get_analysis("lichess", "q7ZvsdUF")
    finally:
        storage.close()
    assert reopened is not None
    assert reopened.opponent == "TryingHard87"
    assert reopened.evaluator.source_kind == "lichess-server"
    assert reopened.evaluator.version is None


def test_supported_flyway_distribution_migrates_empty_and_legacy_databases(
    tmp_path: Path,
) -> None:
    configured_executable = os.environ.get("FLYWAY_TEST_EXECUTABLE")
    if configured_executable is None:
        pytest.skip("set FLYWAY_TEST_EXECUTABLE to run the real Flyway integration")
    executable = Path(configured_executable)
    migrator = FlywayMigrator(MigrationSettings(executable=executable))
    empty_database = tmp_path / "empty.db"
    legacy_database = tmp_path / "legacy.db"
    _legacy_database(legacy_database)

    migrator.migrate(empty_database)
    migrator.migrate(legacy_database)
    migrator.migrate(empty_database)
    migrator.migrate(legacy_database)

    for database_path in (empty_database, legacy_database):
        with sqlite3.connect(database_path) as database:
            assert database.execute("PRAGMA user_version").fetchone() == (4,)
            assert database.execute(
                "SELECT version FROM flyway_schema_history "
                "WHERE success = 1 ORDER BY installed_rank"
            ).fetchall() == [("1",), ("2",), ("3",)]
            primary_key = [
                row[1] for row in database.execute("PRAGMA table_info(games)") if row[5]
            ]
        assert primary_key == ["origin", "origin_id"]
