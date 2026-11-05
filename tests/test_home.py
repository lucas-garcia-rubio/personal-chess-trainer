from collections.abc import Callable
import json
from pathlib import Path
import sqlite3
from dataclasses import replace
from html.parser import HTMLParser

import chess
import pytest

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from trainer.adapters.sqlite import SQLiteStorage
from trainer.bootstrap import create_app
from trainer.config import Settings
from trainer.domain import Analysis, EvaluatorProvenance, GameMetadata


def test_home_is_served_with_a_real_local_database(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("test-player")

    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "rendering Home must not call Lichess"
        ),
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Personal Chess Trainer" in response.text
    assert settings.database_path.is_file()
    with sqlite3.connect(settings.database_path) as database:
        assert database.execute("PRAGMA user_version").fetchone() == (10,)


def test_home_orders_games_by_their_operational_instant(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    older = json.loads(lichess_mock.fixture)
    older["id"] = "older"
    older["createdAt"] = 1_600_000_000_000
    older["players"]["black"]["user"]["name"] = "Older Opponent"
    newer = json.loads(lichess_mock.fixture)
    newer["id"] = "newer"
    newer["createdAt"] = 1_700_000_000_000
    newer["players"]["black"]["user"]["name"] = "Newer Opponent"
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for(
            "Lance5500", ndjson=f"{json.dumps(older)}\n{json.dumps(newer)}"
        ),
    )

    with TestClient(app) as client:
        response = client.post("/sync", follow_redirects=True)

    assert response.text.index("Newer Opponent") < response.text.index("Older Opponent")


def _stored_game(
    *, origin_id: str, opponent: str, played_at: int, created_at: int
) -> tuple[GameMetadata, Analysis]:
    return (
        GameMetadata(
            origin="lichess",
            origin_id=origin_id,
            played_at=played_at,
            white="Player",
            black=opponent,
            result="1-0",
            time_control="600+0",
            eco=None,
            opening=None,
            headers={"Result": "1-0"},
            canonical_document=f"lichess:{origin_id}",
        ),
        Analysis(
            source_id=origin_id,
            created_at=created_at,
            opponent=opponent,
            result="win",
            speed="rapid",
            time_control="600+0",
            player_color="white",
            evaluator=EvaluatorProvenance(
                source_kind="test-double",
                name="deterministic",
                version="1",
                parameters={},
            ),
            critical_moments=[],
        ),
    )


def test_home_orders_by_the_operational_date_not_the_analysis_instant(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("test-player")
    later_metadata, later_analysis = _stored_game(
        origin_id="later",
        opponent="Later Played Opponent",
        played_at=2_000_000_000_000,
        created_at=1_000_000_000_000,
    )
    earlier_metadata, earlier_analysis = _stored_game(
        origin_id="earlier",
        opponent="Earlier Played Opponent",
        played_at=1_000_000_000_000,
        created_at=2_000_000_000_000,
    )
    storage = SQLiteStorage(settings.database_path)
    try:
        storage.save_game("raw-later", later_metadata, later_analysis)
        storage.save_game("raw-earlier", earlier_metadata, earlier_analysis)
    finally:
        storage.close()

    app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "rendering Home must not call Lichess"
        ),
    )
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert (
        response.text.index("Later Played Opponent")
        < response.text.index("Earlier Played Opponent")
    )


class HomeDocument(HTMLParser):
    """Read visible Home links and form controls through the HTTP boundary."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.checked_results: set[str] = set()
        self.analysis_links: list[str] = []
        self.clear_link: str | None = None
        self._link_href: str | None = None
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "result" and "checked" in attributes:
            self.checked_results.add(attributes.get("value") or "")
        if tag == "a":
            href = attributes.get("href") or ""
            if href.startswith("/analyses/"):
                self.analysis_links.append(href)
            self._link_href = href

    def handle_data(self, data: str) -> None:
        if data.strip() == "Limpar filtros":
            self.clear_link = self._link_href

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._link_href = None


@pytest.fixture
def result_games_settings(migrated_settings: Callable[[str], Settings]) -> Settings:
    settings = migrated_settings("Player")
    storage = SQLiteStorage(settings.database_path)
    try:
        # Results belong to the Player, not to White; Analysis creation order
        # deliberately disagrees with the Games' operational instants.
        for index, (result, color, absolute) in enumerate([
            ("win", "black", "0-1"),
            ("loss", "black", "1-0"),
            ("draw", "white", "1/2-1/2"),
            ("loss", "white", "0-1"),
        ]):
            metadata, analysis = _stored_game(
                origin_id=f"result-{index}", opponent=f"Opponent {index}",
                played_at=(index + 1) * 1_000_000_000_000,
                created_at=(4 - index) * 1_000_000_000_000,
            )
            metadata = replace(metadata, result=absolute,
                               headers={"Result": absolute},
                               canonical_document=json.dumps([chess.STARTING_FEN, "e4 e5"]))
            analysis = replace(analysis, result=result, player_color=color)
            storage.save_game("raw", metadata, analysis)
    finally:
        storage.close()
    return settings


def test_home_filters_player_results(
    result_games_settings: Settings, lichess_mock: LichessMock,
) -> None:
    app = create_app(result_games_settings, lichess_transport=lichess_mock.fail_on_request(
        "filtering Home must not call a Game Source"
    ))
    with TestClient(app) as client:
        response = client.get("/?result=loss")
    assert response.status_code == 200
    document = HomeDocument(response.text)
    assert document.checked_results == {"loss"}
    assert "Opponent 3" in response.text
    assert "Opponent 1" in response.text
    assert "Opponent 0" not in response.text
    assert "Opponent 2" not in response.text
    assert "2 de 4 partidas" in response.text
    assert response.text.index("Opponent 3") < response.text.index("Opponent 1")


@pytest.mark.parametrize(("query", "selected", "opponents"), [
    ("", set(), [3, 2, 1, 0]),
    ("?result=win", {"win"}, [0]),
    ("?result=draw", {"draw"}, [2]),
    ("?result=loss", {"loss"}, [3, 1]),
    ("?result=draw&result=loss", {"draw", "loss"}, [3, 2, 1]),
    ("?result=win&result=draw", {"win", "draw"}, [2, 0]),
    ("?result=win&result=loss", {"win", "loss"}, [3, 1, 0]),
    ("?result=win&result=draw&result=loss", {"win", "draw", "loss"}, [3, 2, 1, 0]),
    ("?result=loss&result=loss", {"loss"}, [3, 1]),
    ("?result=invalid&result=", set(), [3, 2, 1, 0]),
])
def test_home_restores_result_selections_and_counts(
    result_games_settings: Settings, lichess_mock: LichessMock,
    query: str, selected: set[str], opponents: list[int],
) -> None:
    app = create_app(result_games_settings, lichess_transport=lichess_mock.fail_on_request(
        "filtering Home must not call a Game Source"
    ))
    with TestClient(app) as client:
        response = client.get("/" + query)
        document = HomeDocument(response.text)
        assert document.checked_results == selected
        assert f"{len(opponents)} de 4 partidas" in response.text
        for index in range(4):
            assert (f"Opponent {index}" in response.text) == (index in opponents)
        positions = [response.text.index(f"Opponent {index}") for index in opponents]
        assert positions == sorted(positions)
        assert len(document.analysis_links) == len(opponents)
        assert (document.clear_link is not None) == bool(selected)
        if selected:
            cleared = client.get(document.clear_link or "/")
            assert "4 de 4 partidas" in cleared.text
            assert HomeDocument(cleared.text).checked_results == set()


def test_home_with_no_matches_preserves_filters_and_clear_action(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Player")
    storage = SQLiteStorage(settings.database_path)
    try:
        metadata, analysis = _stored_game(origin_id="win", opponent="Winning Opponent",
                                         played_at=1000, created_at=1000)
        storage.save_game("raw", metadata, analysis)
    finally:
        storage.close()
    app = create_app(settings, lichess_transport=lichess_mock.fail_on_request(
        "empty searches must not call a Game Source"
    ))
    with TestClient(app) as client:
        response = client.get("/?result=loss&result=draw")
        assert "Nenhuma partida encontrada" in response.text
        assert "0 de 1 partidas" in response.text
        document = HomeDocument(response.text)
        assert document.checked_results == {"loss", "draw"}
        assert document.analysis_links == []
        assert document.clear_link == "/"
        assert "Winning Opponent" in client.get(document.clear_link).text


def test_home_empty_collection_keeps_welcome_and_filters(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    app = create_app(migrated_settings("Player"),
                     lichess_transport=lichess_mock.fail_on_request("Home is local"))
    with TestClient(app) as client:
        response = client.get("/")
        assert "Your next lesson starts here." in response.text
        assert "0 de 0 partidas" in response.text
        assert 'name="result"' in response.text
        assert HomeDocument(response.text).clear_link is None
        filtered = client.get("/?result=loss")
        assert "Nenhuma partida encontrada" in filtered.text
        assert HomeDocument(filtered.text).clear_link == "/"


def test_analysis_home_link_returns_to_filtered_selection(
    result_games_settings: Settings, lichess_mock: LichessMock,
) -> None:
    app = create_app(result_games_settings,
                     lichess_transport=lichess_mock.fail_on_request("Navigation is local"))
    with TestClient(app) as client:
        home = client.get("/?result=draw&result=loss")
        document = HomeDocument(home.text)
        analysis = client.get(document.analysis_links[0])
        assert analysis.status_code == 200
        assert 'href="/?result=draw&amp;result=loss"' in analysis.text
        # A supplied return URL can never turn the Home link into an external link.
        unsafe = client.get("/analyses/lichess/result-3?return_to=https://example.com/")
        assert '<a href="/">← Home</a>' in unsafe.text
