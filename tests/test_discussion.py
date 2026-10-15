from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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


def test_player_continues_a_discussion_and_reopens_its_active_position(
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
        client.post(
            "/analyses/lichess/q7ZvsdUF/discussions",
            data={
                "base_ply": "0",
                "message": "Let me calculate from here.",
                "initial_moves": json.dumps(["e2e4"]),
            },
        )
        continued = client.post(
            "/analyses/lichess/q7ZvsdUF/discussions/0/moves",
            json={"uci": "e7e5", "revision": 1},
        )

    assert continued.status_code == 200
    assert continued.json()["moves"][-1] == {
        "author": "player",
        "uci": "e7e5",
        "san": "e5",
        "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
    }
    assert continued.json()["activeFen"] == (
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    )

    reopened_app = create_app(
        settings,
        lichess_transport=lichess_mock.fail_on_request(
            "a continued Discussion must reopen offline"
        ),
    )
    with TestClient(reopened_app) as client:
        reopened = client.get("/analyses/lichess/q7ZvsdUF?trainer=1&ply=0")

    discussion = cast(
        dict[str, dict[str, object]], _trainer_data(reopened.text)["discussions"]
    )["0"]
    assert discussion["activeFen"] == continued.json()["activeFen"]
    assert discussion["moves"] == continued.json()["moves"]
    assert 'data-navigation-mode="game"' in reopened.text
    assert 'data-navigation-mode="discussion"' in reopened.text
    assert "Discussion · ${currentIndex} of ${positions.length - 1}" in reopened.text


def test_illegal_player_move_leaves_the_discussion_unchanged(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    app = create_app(
        migrated_settings("Lance5500"),
        lichess_transport=lichess_mock.games_for("Lance5500"),
    )
    with TestClient(app) as client:
        client.post("/sync")
        client.post(
            "/analyses/lichess/q7ZvsdUF/discussions",
            data={
                "base_ply": "0",
                "message": "Let me calculate from here.",
                "initial_moves": json.dumps(["e2e4"]),
            },
        )
        before = client.get("/analyses/lichess/q7ZvsdUF")
        rejected = client.post(
            "/analyses/lichess/q7ZvsdUF/discussions/0/moves",
            json={"uci": "e2e5", "revision": 1},
        )
        after = client.get("/analyses/lichess/q7ZvsdUF")

    assert rejected.status_code == 422
    assert rejected.json() == {"detail": "Move is illegal at the active Position"}
    assert _trainer_data(after.text) == _trainer_data(before.text)


def test_branches_remain_selectable_and_active_branch_survives_restart(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    url = "/analyses/lichess/q7ZvsdUF"
    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.games_for("Lance5500")
    )) as client:
        client.post("/sync")
        created = client.post(url + "/discussions", data={
            "base_ply": "0", "message": "Compare these candidates",
            "initial_moves": json.dumps(["e2e4", "e7e5", "g1f3"]),
        })
        original = cast(dict[str, dict[str, object]],
                        _trainer_data(created.text)["discussions"])["0"]
        assert original["revision"] == 1
        original_nodes = original["nodes"]
        nodes = cast(list[dict[str, object]], original_nodes)
        branch = client.post(url + "/discussions/0/moves", json={
            "revision": 1, "parentId": nodes[0]["id"], "uci": "c7c5",
        })
        assert branch.status_code == 200
        branched = branch.json()
        assert branched["revision"] == 2
        assert branched["nodes"][:3] == original_nodes
        assert branched["nodes"][-1]["parentId"] == nodes[0]["id"]
        assert [move["san"] for move in branched["moves"]] == ["e4", "c5"]
        assert branched["activeNodeId"] == branched["nodes"][-1]["id"]
        selected = client.post(url + "/discussions/0/selection", json={
            "revision": 2, "nodeId": nodes[2]["id"],
        })
        assert selected.status_code == 200
        assert selected.json()["revision"] == 3
        assert selected.json()["moves"] == original["moves"]
        assert selected.json()["nodes"] == branched["nodes"]

    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.fail_on_request("reopen offline")
    )) as client:
        reopened = client.get(url + "?trainer=1&ply=0")
        assert 'data-discussion-branches' in reopened.text
        assert 'data-action="delete-discussion"' in reopened.text
        restored = cast(dict[str, dict[str, object]],
                        _trainer_data(reopened.text)["discussions"])["0"]
        assert restored == selected.json()
        alternate = client.post(url + "/discussions/0/selection", json={
            "revision": 3, "nodeId": branched["activeNodeId"],
        })
        assert alternate.status_code == 200
        assert alternate.json()["moves"] == branched["moves"]
        assert alternate.json()["revision"] == 4


def test_conflicting_and_invalid_mutations_leave_no_partial_state(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    url = "/analyses/lichess/q7ZvsdUF"
    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.games_for("Lance5500")
    )) as client:
        client.post("/sync")
        for ply in (0, 1):
            client.post(url + "/discussions", data={
                "base_ply": str(ply), "message": "A candidate",
                "initial_moves": json.dumps(["e2e4"] if ply == 0 else ["e7e5"]),
            })
        before = _trainer_data(client.get(url).text)
        foreign = cast(dict[str, dict[str, object]], before["discussions"])["1"]
        assert client.post(url + "/discussions/0/moves", json={
            "uci": "e7e5",
        }).status_code == 422
        assert client.post(url + "/discussions/0/moves", json={
            "revision": 1, "uci": "g1f3", "parentId": foreign["activeNodeId"],
        }).status_code == 422
        assert client.post(url + "/discussions/0/selection", json={
            "revision": 1, "nodeId": foreign["activeNodeId"],
        }).status_code == 422
        assert _trainer_data(client.get(url).text) == before

        # Independent app connections both act on the same observed revision.
        with TestClient(create_app(
            settings, lichess_transport=lichess_mock.fail_on_request("offline contender")
        )) as contender:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(client.post, url + "/discussions/0/moves",
                                    json={"revision": 1, "uci": "e7e5"})
                second = pool.submit(contender.post, url + "/discussions/0/moves",
                                     json={"revision": 1, "uci": "c7c5"})
                responses = [first.result(), second.result()]
            assert sorted(response.status_code for response in responses) == [200, 409]

        after = _trainer_data(client.get(url).text)
        discussion = cast(dict[str, dict[str, object]], after["discussions"])["0"]
        assert discussion["revision"] == 2
        assert len(cast(list[object], discussion["nodes"])) == 2
        assert discussion["messages"] == [{"author": "player", "body": "A candidate"}]
        assert client.post(url + "/discussions/0/selection", json={
            "revision": 1, "nodeId": None,
        }).status_code == 409
        assert client.post(url + "/discussions/0/moves", json={
            "revision": 1, "parentId": None, "uci": "d2d4",
        }).status_code == 409
        assert _trainer_data(client.get(url).text) == after


def test_confirmed_deletion_clears_only_discussion_and_allows_a_fresh_start(
    migrated_settings: Callable[[str], Settings],
    lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    url = "/analyses/lichess/q7ZvsdUF"
    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.games_for("Lance5500")
    )) as client:
        client.post("/sync")
        game_before = client.get(url).text
        for ply in (0, 1):
            client.post(url + "/discussions", data={
                "base_ply": str(ply), "message": "Delete me" if ply == 0 else "Keep me",
                "initial_moves": json.dumps(["e2e4", "e7e5"] if ply == 0 else []),
            })
        branch = client.post(url + "/discussions/0/moves", json={
            "revision": 1, "parentId": None, "uci": "d2d4",
        })
        assert branch.status_code == 200
        before = _trainer_data(client.get(url).text)
        for body, status_code in (
            ({"revision": 2, "confirmed": False}, 422),
            ({"revision": 2}, 422),
            ({"revision": 1, "confirmed": True}, 409),
        ):
            rejected = client.request("DELETE", url + "/discussions/0", json=body)
            assert rejected.status_code == status_code
            assert _trainer_data(client.get(url).text) == before
        deleted = client.request("DELETE", url + "/discussions/0", json={
            "revision": 2, "confirmed": True,
        })
        assert deleted.status_code == 204

    with TestClient(create_app(
        settings, lichess_transport=lichess_mock.fail_on_request("deletion persists offline")
    )) as client:
        page = client.get(url)
        assert page.status_code == 200
        remaining = cast(dict[str, dict[str, object]],
                         _trainer_data(page.text)["discussions"])
        assert set(remaining) == {"1"}
        assert remaining["1"]["messages"] == [{"author": "player", "body": "Keep me"}]
        assert "q7ZvsdUF" in client.get("/").text
        # The original Analysis and its legal Game navigation are untouched.
        navigation_before = re.search(
            r'<script id="analysis-navigation".*?</script>', game_before, re.DOTALL,
        )
        navigation_after = re.search(
            r'<script id="analysis-navigation".*?</script>', page.text, re.DOTALL,
        )
        assert navigation_before is not None
        assert navigation_after is not None
        assert navigation_after.group() == navigation_before.group()
        fresh = client.post(url + "/discussions", data={
            "base_ply": "0", "message": "Start over", "initial_moves": "[]",
        })
        restarted = cast(dict[str, dict[str, object]],
                         _trainer_data(fresh.text)["discussions"])["0"]
        assert restarted["messages"] == [{"author": "player", "body": "Start over"}]
        assert restarted["nodes"] == []
        assert restarted["moves"] == []
        assert restarted["activeNodeId"] is None
        assert restarted["activeFen"] == restarted["baseFen"]
