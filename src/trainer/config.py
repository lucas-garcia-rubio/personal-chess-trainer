from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class EngineSettings:
    """Effective Stockfish parameters for the local evaluator."""

    executable: Path | None = None
    depth: int = 15
    threads: int = 1
    hash_mb: int = 128


@dataclass(frozen=True)
class Settings:
    lichess_username: str
    database_path: Path
    engine: EngineSettings = EngineSettings()


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
        engine=_load_engine_settings(config, path),
    )


def _load_engine_settings(config: dict[str, object], config_path: Path) -> EngineSettings:
    engine = config.get("engine")
    if not isinstance(engine, dict):
        return EngineSettings()

    configured_executable = engine.get("path")
    if configured_executable is not None:
        if not isinstance(configured_executable, str) or not configured_executable.strip():
            raise ConfigError("Set [engine] path to the Stockfish executable path.")
        executable = Path(configured_executable.strip()).expanduser()
        if not executable.is_absolute():
            executable = config_path.parent / executable
    else:
        executable = None

    return EngineSettings(
        executable=executable,
        depth=_engine_option(engine, "depth", EngineSettings.depth),
        threads=_engine_option(engine, "threads", EngineSettings.threads),
        hash_mb=_engine_option(engine, "hash", EngineSettings.hash_mb),
    )


def _engine_option(engine: dict[str, object], key: str, default: int) -> int:
    value = engine.get(key)
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"Set [engine] {key} to a positive integer.")
    return value
