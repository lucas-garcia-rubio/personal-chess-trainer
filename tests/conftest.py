import pytest

from lichess_mock import LichessMock, load_lichess_fixture


@pytest.fixture
def lichess_mock() -> LichessMock:
    return LichessMock(load_lichess_fixture())
