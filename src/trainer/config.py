from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Settings:
    lichess_username: str
    database_path: Path


class ConfigError(ValueError):
    """Raised when config.toml cannot produce valid application settings."""


def load_settings(path: Path) -> Settings:
    try:
        with path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise ConfigError(
            f"Configuration file not found at {path}. "
            "Create it with [lichess] username and [database] path."
        ) from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Invalid TOML in {path}: {error}") from error
    except OSError as error:
        raise ConfigError(f"Could not read configuration at {path}: {error}") from error

    lichess = config.get("lichess")
    username = lichess.get("username") if isinstance(lichess, dict) else None
    if not isinstance(username, str) or not username.strip():
        raise ConfigError("Set [lichess] username to your Lichess username.")

    database = config.get("database")
    configured_path = database.get("path") if isinstance(database, dict) else None
    if not isinstance(configured_path, str) or not configured_path.strip():
        raise ConfigError("Set [database] path to a writable SQLite file path.")

    database_path = Path(configured_path).expanduser()
    if not database_path.is_absolute():
        database_path = path.parent / database_path

    return Settings(
        lichess_username=username.strip(),
        database_path=database_path,
    )
