import argparse
from collections.abc import Sequence
from pathlib import Path
import sqlite3

import uvicorn

from trainer.bootstrap import create_app
from trainer.config import ConfigError, load_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trainer")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="start the local web application")
    serve.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="configuration file (default: config.toml)",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    arguments = parser.parse_args(argv)

    try:
        settings = load_settings(arguments.config)
    except ConfigError as error:
        parser.error(str(error))

    try:
        app = create_app(settings)
    except (OSError, sqlite3.Error) as error:
        parser.error(
            f"Could not open SQLite database at {settings.database_path}: {error}. "
            "Set [database] path to a writable file."
        )

    uvicorn.run(app, host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()
