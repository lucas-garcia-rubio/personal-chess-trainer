from collections.abc import Callable

from fastapi.testclient import TestClient
from lichess_mock import LichessMock
from test_trainer_stream import StreamingOpenCode, URL, events
from trainer.bootstrap import create_app
from trainer.config import Settings


def test_response_is_one_named_unit_without_a_completion_message(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    with TestClient(create_app(migrated_settings("Lance5500"),
                              lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=StreamingOpenCode())) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Explain", "stream": "true"}).json()
        output = events(client, attempt["eventsUrl"] + "?render=true")
        final = output[-1]["html"]
        assert final.count('aria-label="Trainer response"') == 1
        assert "Consider Nf3." in final
        assert "completed:" not in final
        assert "Trainer attempt" not in final
        assert 'role="status"' in output[0]["html"]
        assert 'role="status"' not in final
        assert client.get(URL + "/0/fragment").text.count('aria-label="Trainer response"') == 1


def test_demonstrated_moves_are_compact_numbered_and_select_their_positions(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    from test_trainer_mcp import ToolOpenCode, call

    runtime = ToolOpenCode()
    with TestClient(create_app(migrated_settings("Lance5500"),
                              lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Show me", "stream": "true"}).json()
        assert runtime.ready.wait(5)
        try:
            before = client.get(URL + "/0").json()
            assert not call(client, runtime.token, "play_line", {
                "attempt_id": attempt["id"], "revision": before["revision"],
                "active_node_id": None, "moves": ["e2e4", "e7e5", "g1f3"],
            })["isError"]
        finally:
            runtime.release.set()
        events(client, attempt["eventsUrl"])
        fragment = client.get(URL + "/0/fragment").text
        assert fragment.count('aria-label="Demonstrated line"') == 1
        assert '>1. e4</button>' in fragment
        assert '>1... e5</button>' in fragment
        assert '>2. Nf3</button>' in fragment
        assert "Trainer move:" not in fragment
        assert "branch " not in fragment
        saved = client.get(URL + "/0").json()
        for node in saved["nodes"]:
            selected = client.post(URL + "/0/selection", json={
                "revision": saved["revision"], "nodeId": node["id"],
            }).json()
            assert selected["activeFen"] == node["fen"]
            assert selected["nodes"] == saved["nodes"]
            saved = selected


def test_interrupted_response_and_retry_keep_separate_lines_and_readable_branches(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    from test_trainer_mcp import ToolOpenCode, call

    runtime = ToolOpenCode()
    settings = migrated_settings("Lance5500")
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Compare", "stream": "true"}).json()
        assert runtime.ready.wait(5)
        before = client.get(URL + "/0").json()
        assert not call(client, runtime.token, "play_line", {
            "attempt_id": attempt["id"], "revision": before["revision"],
            "active_node_id": None, "moves": ["e2e4", "e7e5"],
        })["isError"]
        client.post(URL + f'/0/attempts/{attempt["id"]}/interrupt')
        events(client, attempt["eventsUrl"])
        runtime.ready.clear()
        runtime.release.clear()
        retry = client.post(URL + f'/0/attempts/{attempt["id"]}/retry').json()
        assert runtime.ready.wait(5)
        try:
            before = client.get(URL + "/0").json()
            assert not call(client, runtime.token, "play_line", {
                "attempt_id": retry["id"], "revision": before["revision"],
                "active_node_id": None, "moves": ["d2d4", "d7d5"],
            })["isError"]
        finally:
            runtime.release.set()
        output = events(client, retry["eventsUrl"] + "?render=true")
        fragment = client.get(URL + "/0/fragment").text
        assert fragment.count('aria-label="Trainer response"') == 2
        assert fragment.count('aria-label="Demonstrated line"') == 2
        assert fragment.count('aria-label="Variation"') == 2
        assert "Response interrupted." in fragment
        assert fragment.count("New attempt") == 1
        assert fragment.index('>1. e4</button>') < fragment.index("New attempt") < fragment.index('>1. d4</button>')
        assert output[0]["html"].count('aria-label="Demonstrated line"') == 1
        raw_events = events(client, retry["eventsUrl"])
        for event in output:
            assert client.get(
                URL + f'/0/fragment?attempt_id={retry["id"]}&cursor={event["sequence"]}'
            ).text == event["html"]
        saved = client.get(URL + "/0").json()
    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"))) as client:
        assert client.get(URL + "/0/fragment").text == fragment
        assert client.get(URL + "/0").json() == saved
        assert events(client, retry["eventsUrl"]) == raw_events
        replay = events(client, retry["eventsUrl"] + "?render=true")
        # Live polling may emit more text updates than a completed replay.
        assert replay[-1] == output[-1]
        assert [event for event in replay if event["type"] != "text"] == [
            event for event in output if event["type"] != "text"]


def test_explanations_and_two_demonstrations_keep_their_order_in_one_response(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    from collections.abc import Iterator
    from threading import Event
    from test_trainer_mcp import ToolOpenCode, call
    from trainer.application.ports import ResponseControl, TrainerEvent

    class InterleavedRuntime(ToolOpenCode):
        middle = Event()
        finish = Event()

        def stream(self, session_id: str, system: str, context: str,
                   control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
            yield TrainerEvent("text", "First develop the center. " * 100)
            self.ready.set()
            assert self.release.wait(10)
            yield TrainerEvent("text", "Then develop the knights.")
            self.middle.set()
            assert self.finish.wait(10)
            yield TrainerEvent("text", "Now compare the resulting Position.")
            yield TrainerEvent("completed")

    runtime = InterleavedRuntime()
    with TestClient(create_app(migrated_settings("Lance5500"),
                              lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Explain", "stream": "true"}).json()
        try:
            for ready, release, moves in [(runtime.ready, runtime.release, ["e2e4", "e7e5"]),
                                           (runtime.middle, runtime.finish, ["g1f3", "b8c6"])]:
                assert ready.wait(5)
                state = client.get(URL + "/0").json()
                assert not call(client, runtime.token, "play_line", {
                    "attempt_id": attempt["id"], "revision": state["revision"],
                    "active_node_id": state["activeNodeId"], "moves": moves,
                })["isError"]
                release.set()
        finally:
            runtime.release.set()
            runtime.finish.set()
        output = events(client, attempt["eventsUrl"] + "?render=true")
        final = output[-1]["html"]
        assert final.count('aria-label="Trainer response"') == 1
        assert final.count('aria-label="Demonstrated line"') == 2
        assert final.index("First develop") < final.index('>1. e4</button>') < final.index(
            "Then develop") < final.index('>2. Nf3</button>') < final.index("Now compare")
        first_line = next(event["html"] for event in output if event["type"] == "move")
        assert "Then develop" not in first_line
        assert '>2. Nf3</button>' not in first_line
