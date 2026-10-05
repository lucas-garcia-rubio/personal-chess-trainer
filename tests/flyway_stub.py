#!/usr/bin/env python3
from pathlib import Path
import sqlite3
import sys


def option(prefix: str) -> str:
    return next(argument.removeprefix(prefix) for argument in sys.argv[1:] if argument.startswith(prefix))


command = sys.argv[-1]
if command == "version":
    print("Flyway OSS Edition 13.9.0 by Redgate")
    raise SystemExit(0)

database_path = Path(option("-url=jdbc:sqlite:"))
migration_path = Path(option("-locations=filesystem:"))
with sqlite3.connect(database_path) as database:
    if command == "baseline":
        database.execute(
            "CREATE TABLE flyway_schema_history (version TEXT PRIMARY KEY)"
        )
        database.execute("INSERT INTO flyway_schema_history VALUES ('1')")
    elif command == "migrate":
        database.execute(
            "CREATE TABLE IF NOT EXISTS flyway_schema_history "
            "(version TEXT PRIMARY KEY)"
        )
        applied = {
            str(row[0])
            for row in database.execute("SELECT version FROM flyway_schema_history")
        }
        for migration in sorted(migration_path.glob("V*__*.sql")):
            version = migration.name.split("__", maxsplit=1)[0].removeprefix("V")
            if version not in applied:
                database.executescript(migration.read_text(encoding="utf-8"))
                database.execute(
                    "INSERT INTO flyway_schema_history VALUES (?)", (version,)
                )
