from pathlib import Path

import pytest

from trainer.config import ConfigError, Settings, load_settings


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
    )


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
