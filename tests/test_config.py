from pathlib import Path

import pytest

from trainer.config import (
    ConfigError,
    EngineSettings,
    MigrationSettings,
    Settings,
    load_settings,
)


def test_load_settings_reads_toml_and_resolves_database_path(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[lichess]\nusername = "test-player"\n\n[database]\npath = "data/trainer.db"\n',
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings == Settings(
        lichess_username="test-player",
        database_path=tmp_path / "data" / "trainer.db",
        engine=EngineSettings(),
        migration=MigrationSettings(),
    )


def test_load_settings_reads_engine_configuration_and_resolves_relative_path(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[lichess]\nusername = "test-player"\n\n'
        '[database]\npath = "data/trainer.db"\n\n'
        '[engine]\npath = "engines/stockfish"\ndepth = 20\nthreads = 4\n'
        'hash = 256\nmax_plies = 800\ntimeout_seconds = 300\n',
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.engine == EngineSettings(
        executable=tmp_path / "engines" / "stockfish",
        depth=20,
        threads=4,
        hash_mb=256,
        max_plies=800,
        timeout_seconds=300,
    )


def test_load_settings_keeps_stockfish_on_path_when_no_engine_path_is_configured(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[lichess]\nusername = "test-player"\n\n'
        '[database]\npath = "data/trainer.db"\n\n'
        '[engine]\ndepth = 18\n',
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.engine.executable is None
    assert settings.engine.depth == 18
    assert settings.engine.threads == 1
    assert settings.engine.hash_mb == 128
    assert settings.engine.max_plies == 1000
    assert settings.engine.timeout_seconds == 600


def test_load_settings_reads_and_resolves_the_flyway_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[lichess]\nusername = "test-player"\n\n'
        '[database]\npath = "data/trainer.db"\n\n'
        '[migration]\nflyway_path = "tools/flyway"\n',
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.migration == MigrationSettings(
        executable=tmp_path / "tools" / "flyway"
    )


@pytest.mark.parametrize(
    ("engine_section", "expected_message"),
    [
        ("[engine]\npath = 42\n", "Set [engine] path"),
        ("[engine]\ndepth = 0\n", "Set [engine] depth to a positive integer"),
        ("[engine]\ndepth = true\n", "Set [engine] depth to a positive integer"),
        ("[engine]\ndepth = 129\n", "Set [engine] depth between 1 and 128"),
        ("[engine]\nthreads = -2\n", "Set [engine] threads to a positive integer"),
        ("[engine]\nthreads = 1025\n", "Set [engine] threads between 1 and 1024"),
        ('[engine]\nhash = "big"\n', "Set [engine] hash to a positive integer"),
        ("[engine]\nhash = 33554433\n", "Set [engine] hash between 1 and 33554432"),
        ("[engine]\nmax_plies = 0\n", "Set [engine] max_plies to a positive integer"),
        ("[engine]\nmax_plies = 10001\n", "Set [engine] max_plies between 1 and 10000"),
        ("[engine]\ntimeout_seconds = false\n", "Set [engine] timeout_seconds to a positive integer"),
        ("[engine]\ntimeout_seconds = 3601\n", "Set [engine] timeout_seconds between 1 and 3600"),
        ('[migration]\nflyway_path = 42\n', "Set [migration] flyway_path"),
    ],
)
def test_load_settings_rejects_invalid_engine_configuration(
    tmp_path: Path,
    engine_section: str,
    expected_message: str,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[lichess]\nusername = "test-player"\n\n'
        '[database]\npath = "data/trainer.db"\n\n' + engine_section,
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_settings(config_path)

    assert expected_message in str(error.value)


@pytest.mark.parametrize(
    ("contents", "expected_message"),
    [
        (None, "Create it with [lichess] username and [database] path"),
        ('[lichess]\nusername = ""\n', "Set [lichess] username"),
        (
            '[lichess]\nusername = "test-player"\n[database]\npath = 42\n',
            "Set [database] path",
        ),
    ],
)
def test_load_settings_explains_how_to_fix_invalid_configuration(
    tmp_path: Path,
    contents: str | None,
    expected_message: str,
) -> None:
    config_path = tmp_path / "config.toml"
    if contents is not None:
        config_path.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        load_settings(config_path)

    assert expected_message in str(error.value)
