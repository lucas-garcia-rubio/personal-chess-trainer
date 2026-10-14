from collections.abc import Callable
from html import unescape
import json
import re
from typing import cast

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from trainer.bootstrap import create_app
from trainer.config import Settings


def _trainer_data(html: str) -> dict[str, object]:
    match = re.search(
        r'<script id="trainer-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return cast(dict[str, object], json.loads(unescape(match.group(1))))


def test_analysis_offers_trainer_without_creating_a_discussion(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.games_for("Lance5500"),
    )
    with TestClient(app) as client:
        client.post("/sync")
        response = client.get("/analyses/lichess/q7ZvsdUF")

    assert response.status_code == 200
    assert 'role="tab">Analysis</button>' in response.text
    assert 'role="tab">Trainer</button>' in response.text
    assert 'action="/analyses/lichess/q7ZvsdUF/discussions"' in response.text
    assert 'name="base_ply"' in response.text
    assert 'name="initial_moves"' in response.text
    assert "exploredMoves.push(chess.history({ verbose: true }).at(-1).lan)" in response.text
    assert _trainer_data(response.text) == {"discussions": {}}

    reopened_app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "browsing an Analysis must not create a Discussion"
        ),
    )
    with TestClient(reopened_app) as client:
        reopened = client.get("/analyses/lichess/q7ZvsdUF")

    assert _trainer_data(reopened.text) == {"discussions": {}}


def test_first_message_persists_its_base_position_and_explored_line(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.games_for("Lance5500"),
    )
    with TestClient(app) as client:
        client.post("/sync")
        created = client.post(
            "/analyses/lichess/q7ZvsdUF/discussions",
            data={
                "base_ply": "0",
                "message": "What should I fight for here?",
                "initial_moves": json.dumps(["e2e4", "e7e5", "g1f3"]),
            },
            follow_redirects=False,
        )

    assert created.status_code == 303

    reopened_app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "a persisted Discussion must reopen offline"
        ),
    )
    with TestClient(reopened_app) as client:
        response = client.get("/analyses/lichess/q7ZvsdUF")

    discussions = cast(dict[str, dict[str, object]], _trainer_data(response.text)["discussions"])
    discussion = discussions["0"]
    assert discussion["baseFen"] == (
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    )
    assert discussion["messages"] == [
        {"author": "player", "body": "What should I fight for here?"}
    ]
    assert discussion["moves"] == [
        {
            "author": "player",
            "uci": "e2e4",
            "san": "e4",
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        },
        {
            "author": "player",
            "uci": "e7e5",
            "san": "e5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
        },
        {
            "author": "player",
            "uci": "g1f3",
            "san": "Nf3",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
        },
    ]


def test_illegal_initial_line_persists_neither_message_nor_moves(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500"),
    )
    with TestClient(app) as client:
        client.post("/sync")
        rejected = client.post(
            "/analyses/lichess/q7ZvsdUF/discussions",
            data={
                "base_ply": "0",
                "message": "Does this line work?",
                "initial_moves": json.dumps(["e2e4", "e7e4"]),
            },
        )
        analysis = client.get("/analyses/lichess/q7ZvsdUF")

    assert rejected.status_code == 422
    assert _trainer_data(analysis.text) == {"discussions": {}}


def test_repeated_positions_at_different_game_plies_have_separate_discussions(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    document = json.loads(lichess_mock.fixture)
    document["id"] = "repeated-position"
    document["moves"] = "Nf3 Nf6 Ng1 Ng8"
    document["analysis"] = document["analysis"][:4]
    settings = migrated_settings("Lance5500")
    app = create_app(
        settings,
        lichess_transport=lichess_mock.games_for(
            "Lance5500", ndjson=json.dumps(document)
        ),
    )
    with TestClient(app) as client:
        client.post("/sync")
        for ply, message in ((0, "At the start"), (4, "After the repetition")):
            response = client.post(
                "/analyses/lichess/repeated-position/discussions",
                data={
                    "base_ply": str(ply),
                    "message": message,
                    "initial_moves": "[]",
                },
            )
            assert response.status_code == 200

        duplicate = client.post(
            "/analyses/lichess/repeated-position/discussions",
            data={
                "base_ply": "0",
                "message": "This must not create another Discussion",
                "initial_moves": "[]",
            },
        )
        analysis = client.get("/analyses/lichess/repeated-position")

    assert duplicate.status_code == 200
    discussions = cast(
        dict[str, dict[str, object]], _trainer_data(analysis.text)["discussions"]
    )
    assert set(discussions) == {"0", "4"}
    assert discussions["0"]["messages"] == [
        {"author": "player", "body": "At the start"}
    ]
    assert discussions["4"]["messages"] == [
        {"author": "player", "body": "After the repetition"}
    ]
    assert str(discussions["0"]["baseFen"]).split()[:4] == str(
        discussions["4"]["baseFen"]
    ).split()[:4]
