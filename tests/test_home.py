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
        assert database.execute("PRAGMA user_version").fetchone() == (11,)


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
        self.time_controls: dict[str, str] = {}
        self.checked_time_controls: set[str] = set()
        self.categories: dict[str, str] = {}
        self.checked_categories: set[str] = set()
        self._category: str | None = None
        self._control: str | None = None
        self.analysis_links: list[str] = []
        self.clear_link: str | None = None
        self._link_href: str | None = None
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "result" and "checked" in attributes:
            self.checked_results.add(attributes.get("value") or "")
        if tag == "input" and attributes.get("name") == "time_control":
            self._control = attributes.get("value") or ""
            self.time_controls[self._control] = ""
            if "checked" in attributes:
                self.checked_time_controls.add(self._control)
        if tag == "input" and attributes.get("name") == "category":
            self._category = attributes.get("value") or ""
            self.categories[self._category] = ""
            if "checked" in attributes:
                self.checked_categories.add(self._category)
        if tag == "a":
            href = attributes.get("href") or ""
            if href.startswith("/analyses/"):
                self.analysis_links.append(href)
            self._link_href = href

    def handle_data(self, data: str) -> None:
        if self._category is not None:
            self.categories[self._category] += data.strip()
        if self._control is not None:
            self.time_controls[self._control] += data.strip()
        if data.strip() == "Limpar filtros":
            self.clear_link = self._link_href

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self._control = None
            self._category = None
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


@pytest.fixture
def time_games_settings(migrated_settings: Callable[[str], Settings]) -> Settings:
    settings = migrated_settings("Player")
    storage = SQLiteStorage(settings.database_path)
    try:
        for index, (control, result) in enumerate([
            ("600+0", "loss"), ("600+5", "draw"), ("600+0", "win"),
            ("180+0", "loss"), ("600+5", "loss"), ("600+0", "draw"),
            ("-", "win"), ("?", "draw"), ("", "loss"), ("unknown", "draw"),
            ("40/7200:3600", "draw"), ("30+1", "win"), ("61+2", "loss"),
        ]):
            metadata, analysis = _stored_game(
                origin_id=f"time-{index}", opponent=f"Clock Opponent {index}",
                played_at=(index + 1) * 1000, created_at=(20 - index) * 1000,
            )
            metadata = replace(metadata, time_control=control,
                               canonical_document=json.dumps([chess.STARTING_FEN, "e4 e5"]))
            storage.save_game("raw", metadata, replace(analysis, time_control=control, result=result))
    finally:
        storage.close()
    return settings


def test_home_combines_exact_controls_with_player_results(
    time_games_settings: Settings, lichess_mock: LichessMock,
) -> None:
    app = create_app(time_games_settings, lichess_transport=lichess_mock.fail_on_request("Home is local"))
    with TestClient(app) as client:
        response = client.get("/?time_control=600%2B0&time_control=600%2B5&result=loss&result=draw")
    document = HomeDocument(response.text)
    assert document.checked_time_controls == {"600+0", "600+5"}
    assert document.checked_results == {"loss", "draw"}
    assert [link.split("?")[0] for link in document.analysis_links] == [
        f"/analyses/lichess/time-{index}" for index in [5, 4, 1, 0]
    ]
    assert "4 de 13 partidas" in response.text
    assert document.time_controls["600+0"] == "10+0"
    assert document.time_controls["600+5"] == "10+5"


@pytest.mark.parametrize(("controls", "results", "expected"), [
    ([], [], list(reversed(range(13)))),
    (["600+0", "600+5"], [], [5, 4, 2, 1, 0]),
    ([], ["loss"], [12, 8, 4, 3, 0]),
    (["600+5"], ["win"], []),
    (["-"], [], [6]), (["?"], [], [7]), ([""], [], [8]),
    (["unknown"], [], [9]), (["40/7200:3600"], [], [10]),
    (["600+0", "600+0"], ["draw"], [5]),
])
def test_home_restores_controls_options_and_empty_searches(
    time_games_settings: Settings, lichess_mock: LichessMock,
    controls: list[str], results: list[str], expected: list[int],
) -> None:
    from urllib.parse import urlencode
    app = create_app(time_games_settings, lichess_transport=lichess_mock.fail_on_request("Home is local"))
    with TestClient(app) as client:
        response = client.get("/?" + urlencode({"time_control": controls, "result": results}, doseq=True))
        document = HomeDocument(response.text)
        assert document.checked_time_controls == set(controls)
        assert document.checked_results == set(results)
        assert [link.split("?")[0] for link in document.analysis_links] == [
            f"/analyses/lichess/time-{index}" for index in expected
        ]
        assert f"{len(expected)} de 13 partidas" in response.text
        assert document.time_controls == {
            "600+0": "10+0", "600+5": "10+5", "180+0": "3+0",
            "-": "Desconhecido", "?": "Desconhecido", "": "Desconhecido",
            "unknown": "Desconhecido", "40/7200:3600": "40/7200:3600",
            "30+1": "0.5+1", "61+2": "61/60+2",
        }
        assert ("Nenhuma partida encontrada" in response.text) == (not expected)
        assert bool(document.clear_link) == bool(controls or results)
        if controls or results:
            cleared = HomeDocument(client.get(document.clear_link or "/").text)
            assert len(cleared.analysis_links) == 13
            assert not cleared.checked_results
            assert not cleared.checked_time_controls
        if document.analysis_links:
            analysis = client.get(document.analysis_links[0])
            assert analysis.status_code == 200


def test_home_formats_large_controls_without_losing_seconds(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Player")
    storage = SQLiteStorage(settings.database_path)
    try:
        metadata, analysis = _stored_game(origin_id="large", opponent="Large clock",
                                         played_at=1000, created_at=1000)
        storage.save_game("raw", replace(metadata, time_control="600000000000000003+5"), analysis)
    finally:
        storage.close()
    app = create_app(settings, lichess_transport=lichess_mock.fail_on_request("Home is local"))
    with TestClient(app) as client:
        document = HomeDocument(client.get("/").text)
        assert document.time_controls["600000000000000003+5"] == "10000000000000000.05+5"


@pytest.fixture
def category_games_settings(migrated_settings: Callable[[str], Settings]) -> Settings:
    settings = migrated_settings("Player")
    storage = SQLiteStorage(settings.database_path)
    try:
        for index, (category, result, control) in enumerate([
            ("blitz", "loss", "600+0"), ("rapid", "draw", "600+5"),
            ("blitz", "win", "600+5"), ("bullet", "loss", "600+0"),
            ("unknown", "draw", "600+0"), ("", "loss", "600+5"),
            ("ultraBullet", "win", "600+0"), ("classical", "draw", "600+5"),
            ("correspondence", "loss", "600+0"),
        ]):
            metadata, analysis = _stored_game(
                origin_id=f"category-{index}", opponent=f"Category Opponent {index}",
                played_at=(index + 1) * 1000, created_at=(20 - index) * 1000,
            )
            metadata = replace(metadata, time_control=control,
                               canonical_document=json.dumps([chess.STARTING_FEN, "e4 e5"]))
            # Deliberately unrelated clocks: categories must never be recalculated.
            storage.save_game("raw", metadata, replace(analysis, speed=category, result=result))
    finally:
        storage.close()
    return settings


def test_home_combines_persisted_categories_with_player_results(
    category_games_settings: Settings, lichess_mock: LichessMock,
) -> None:
    app = create_app(category_games_settings, lichess_transport=lichess_mock.fail_on_request("Home is local"))
    with TestClient(app) as client:
        response = client.get("/?time_mode=category&category=blitz&category=rapid&result=loss&result=draw")
    document = HomeDocument(response.text)
    assert document.checked_categories == {"blitz", "rapid"}
    assert document.checked_results == {"loss", "draw"}
    assert not document.time_controls
    assert [link.split("?")[0] for link in document.analysis_links] == [
        "/analyses/lichess/category-1", "/analyses/lichess/category-0",
    ]
    assert "2 de 9 partidas" in response.text
    assert document.categories == {
        "": "Desconhecida", "blitz": "Blitz", "bullet": "Bullet",
        "classical": "Classical", "correspondence": "Correspondence",
        "rapid": "Rapid", "ultraBullet": "UltraBullet", "unknown": "Desconhecida",
    }


@pytest.mark.parametrize(("mode", "active", "inactive", "expected"), [
    ("category", ("category", "blitz"), ("time_control", "600+5"), [2, 0]),
    ("exact", ("time_control", "600+0"), ("category", "rapid"), [8, 6, 4, 3, 0]),
])
def test_home_removes_inactive_time_criteria_from_direct_urls(
    category_games_settings: Settings, lichess_mock: LichessMock,
    mode: str, active: tuple[str, str], inactive: tuple[str, str], expected: list[int],
) -> None:
    from urllib.parse import parse_qs, urlencode, urlsplit
    app = create_app(category_games_settings, lichess_transport=lichess_mock.fail_on_request("Home is local"))
    with TestClient(app) as client:
        response = client.get("/?" + urlencode([("time_mode", mode), active, inactive]))
        query = parse_qs(urlsplit(str(response.url)).query)
        assert inactive[0] not in query
        assert query[active[0]] == [active[1]]
        document = HomeDocument(response.text)
        assert [link.split("?")[0] for link in document.analysis_links] == [
            f"/analyses/lichess/category-{index}" for index in expected
        ]
        assert document.checked_results == set()
        assert (document.checked_categories if mode == "category" else document.checked_time_controls) == {active[1]}


@pytest.mark.parametrize(("categories", "results", "expected"), [
    ([], [], [8, 7, 6, 5, 4, 3, 2, 1, 0]),
    (["blitz", "rapid"], [], [2, 1, 0]),
    (["blitz", "rapid"], ["loss", "draw"], [1, 0]),
    (["blitz", "blitz"], ["win"], [2]),
    (["bullet"], [], [3]), (["unknown"], [], [4]), ([""], [], [5]),
    (["ultraBullet", "correspondence"], [], [8, 6]),
    (["bullet"], ["draw"], []),
    ([], ["draw"], [7, 4, 1]),
])
def test_home_category_options_stay_available_for_every_selection(
    category_games_settings: Settings, lichess_mock: LichessMock,
    categories: list[str], results: list[str], expected: list[int],
) -> None:
    from urllib.parse import urlencode
    app = create_app(category_games_settings, lichess_transport=lichess_mock.fail_on_request("Home is local"))
    query = urlencode({"time_mode": "category", "category": categories, "result": results}, doseq=True)
    with TestClient(app) as client:
        response = client.get("/?" + query)
        document = HomeDocument(response.text)
        assert document.checked_categories == set(categories)
        assert document.checked_results == set(results)
        assert set(document.categories) == {
            "blitz", "rapid", "bullet", "unknown", "", "ultraBullet", "classical", "correspondence",
        }
        assert not document.time_controls
        assert [link.split("?")[0] for link in document.analysis_links] == [
            f"/analyses/lichess/category-{index}" for index in expected
        ]
        assert f"{len(expected)} de 9 partidas" in response.text
        assert ("Nenhuma partida encontrada" in response.text) == (not expected)
        assert bool(document.clear_link) == bool(categories or results)
        if categories or results:
            cleared = HomeDocument(client.get(document.clear_link or "/").text)
            assert len(cleared.analysis_links) == 9
            assert not cleared.checked_categories
            assert not cleared.checked_results
        if document.analysis_links:
            analysis = client.get(document.analysis_links[0])
            assert analysis.status_code == 200
            assert f'href="/?{query.replace("&", "&amp;")}"' in analysis.text


def test_category_mode_with_empty_library_keeps_tabs_and_no_restriction(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    app = create_app(migrated_settings("Player"),
                     lichess_transport=lichess_mock.fail_on_request("Home is local"))
    with TestClient(app) as client:
        response = client.get("/?time_mode=category")
        document = HomeDocument(response.text)
        assert "0 de 0 partidas" in response.text
        assert "Your next lesson starts here." in response.text
        assert document.clear_link is None
        assert not document.categories
        assert not document.time_controls
        assert 'role="tab" aria-selected="true"' in response.text
        assert "Trocar o modo limpa a seleção de tempo" in response.text


def test_home_progressively_enhances_filters_with_local_htmx(
    result_games_settings: Settings, lichess_mock: LichessMock,
) -> None:
    app = create_app(
        result_games_settings,
        lichess_transport=lichess_mock.fail_on_request("Home filters stay local"),
    )
    with TestClient(app) as client:
        response = client.get("/")
        assert 'onchange=' not in response.text
        assert 'hx-get="/"' in response.text
        assert 'hx-trigger="change, submit"' in response.text
        assert 'hx-push-url="true"' in response.text
        assert 'type="submit">Aplicar filtros</button>' in response.text
        assert '<script src="/static/htmx.min.js" defer></script>' in response.text
        script = client.get("/static/htmx.min.js")
        assert script.status_code == 200
        assert "htmx" in script.text
        assert "javascript" in script.headers["content-type"]
