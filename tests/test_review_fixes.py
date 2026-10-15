from collections.abc import Callable, Iterator
from dataclasses import replace
import json
from pathlib import Path

from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from test_practical_line import PracticalOpenCode
from test_trainer_evidence import EvidenceEvaluator
from test_trainer_stream import URL, events
from test_trainer_mcp import call
from trainer.bootstrap import create_app
from trainer.config import EngineSettings, Settings
from trainer.application.ports import ResponseControl, TrainerEvent


def test_text_selects_displayed_node_and_rejects_stale_or_invalid_requests(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    runtime = PracticalOpenCode()
    runtime.release.set()
    with TestClient(create_app(migrated_settings("Lance5500"),
                              lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        first = client.post(URL, data={"base_ply": "0", "message": "Explore",
                                      "initial_moves": '["e2e4", "e7e5"]', "stream": "true"}).json()
        events(client, first["eventsUrl"])
        saved = client.get(URL + "/0").json()
        node = saved["nodes"][0]
        for position in [{"kind": "variation", "node_id": node["id"]}, {"kind": "game", "ply": 0}]:
            before = client.get(URL + "/0").json()
            response = client.post(URL, data={"base_ply": "0", "message": "This position?", "stream": "true",
                "revision": str(before["revision"]), "position": json.dumps(position)})
            assert response.status_code == 202
            events(client, response.json()["eventsUrl"])
            assert runtime.context["active_position"] == (node["fen"] if position["kind"] == "variation" else saved["baseFen"])
        before = client.get(URL + "/0").json()
        for revision, position, expected in [
            (before["revision"] - 1, {"kind": "game", "ply": 0}, 409),
            (before["revision"], {"kind": "variation", "node_id": 99999}, 422),
        ]:
            assert client.post(URL, data={"base_ply": "0", "message": "Rejected", "stream": "true",
                "revision": str(revision), "position": json.dumps(position)}).status_code == expected
            assert client.get(URL + "/0").json() == before
        # Returning to Game branches immediately from the Base Position.
        branch = client.post(URL + "/0/moves", json={"revision": before["revision"], "parentId": None, "uci": "d2d4"})
        assert branch.status_code == 200
        assert [n["uci"] for n in branch.json()["nodes"]] == ["e2e4", "e7e5", "d2d4"]


def test_best_line_validates_transient_exploration_and_adopts_its_player_prefix(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    evaluator = EvidenceEvaluator()
    with TestClient(create_app(migrated_settings("Lance5500"),
                              lichess_transport=lichess_mock.games_for("Lance5500"),
                              candidate_evaluator=evaluator)) as client:
        client.post("/sync")
        endpoint = "/analyses/lichess/q7ZvsdUF/best-line"
        bad = client.post(endpoint, json={"ply": 0, "position": {
            "kind": "exploration", "ply": 0, "moves": ["e2e4", "e7e4"]}})
        assert bad.status_code == 422
        assert evaluator.requests == []
        response = client.post(endpoint, json={"ply": 0, "position": {
            "kind": "exploration", "ply": 0, "moves": ["e2e4"]}})
        assert response.status_code == 200
        preview = response.json()
        assert preview["startingFen"] == "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
        assert evaluator.requests == [(preview["startingFen"], 3)]
        assert client.get(URL + "/0").status_code == 404
        adopted = client.post(URL + "/0/adopt-preview", json={"previewId": preview["previewId"], "plies": 1}).json()
        assert adopted["nodes"][0]["author"] == "player"
        assert adopted["nodes"][0]["uci"] == "e2e4"
        assert adopted["nodes"][1]["author"] == "evaluator"


def test_production_missing_stockfish_disables_best_line_with_diagnosis(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    settings = replace(migrated_settings("Lance5500"), engine=EngineSettings(executable=Path("/missing/stockfish")))
    runtime = PracticalOpenCode()
    runtime.release.set()
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime)) as client:
        client.post("/sync")
        page = client.get("/analyses/lichess/q7ZvsdUF").text
        assert 'data-action="best-line" disabled' in page
        assert "Stockfish executable" in page
        assert client.post("/analyses/lichess/q7ZvsdUF/best-line", json={"ply": 0}).status_code == 503
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post("/analyses/lichess/q7ZvsdUF/practical-line", json={"ply": 0}).json()
        events(client, attempt["eventsUrl"])
        assert runtime.context["practical_line"]["evidence"]["status"] == "unavailable"


def test_fragments_replay_text_moves_and_evidence_only_through_processed_cursor(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    class OrderedOpenCode(PracticalOpenCode):
        def stream(self, session_id: str, system: str, context: str,
                   control: ResponseControl | None = None) -> Iterator[TrainerEvent]:
            yield TrainerEvent("text", "Before demonstration.")
            yield from super().stream(session_id, system, context, control)

    runtime = OrderedOpenCode()
    with TestClient(create_app(migrated_settings("Lance5500"),
                              lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime, candidate_evaluator=EvidenceEvaluator())) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Show me", "stream": "true"}).json()
        assert runtime.ready.wait(5)
        try:
            before = client.get(URL + "/0").json()
            assert not call(client, runtime.token, "play_line", {"attempt_id": attempt["id"],
                "revision": before["revision"], "active_node_id": None, "moves": ["e2e4", "e7e5"]})["isError"]
            assert not call(client, runtime.token, "evaluate_position", {"attempt_id": attempt["id"],
                "position": {"kind": "variation", "node_id": client.get(URL + "/0").json()["activeNodeId"]}})["isError"]
        finally:
            runtime.release.set()
        output = events(client, attempt["eventsUrl"])
        fragment_url = URL + f'/0/fragment?attempt_id={attempt["id"]}&cursor='
        # All generation has finished before replay: latest-state snapshots would leak.
        early = client.get(fragment_url + "1").text
        assert "Trainer move:" not in early and "Evaluator evidence" not in early
        assert "clear development plan" not in early and '"nodes": []' in early
        move = client.get(fragment_url + "3").text
        assert "Trainer move: e4" in move and "Trainer move: e5" not in move
        assert "Evaluator evidence" not in move and "clear development plan" not in move
        final = client.get(fragment_url + str(output[-1]["sequence"])).text
        assert final.index("Before demonstration.") < final.index("Trainer move: e4") < final.index("Trainer move: e5") < final.index("Evaluator evidence") < final.index("clear development plan")
        assert client.get(URL + "/0/fragment?cursor=2").status_code == 422
        latest = client.get(URL + "/0").json()
        assert client.post(URL + "/0/moves", json={"revision": latest["revision"],
            "parentId": None, "uci": "d2d4"}).status_code == 200
        assert client.get(fragment_url + "1").text == early


def test_retry_keeps_original_displayed_position_and_request_after_partial_moves(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    runtime = PracticalOpenCode()
    with TestClient(create_app(migrated_settings("Lance5500"),
                              lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Why this position?", "stream": "true",
            "position": json.dumps({"kind": "exploration", "ply": 0, "moves": ["e2e4"]}),
            "revision": "null"}).json()
        assert runtime.ready.wait(5)
        before = client.get(URL + "/0").json()
        original_fen = runtime.context["active_position"]
        try:
            assert not call(client, runtime.token, "play_line", {"attempt_id": attempt["id"],
                "revision": before["revision"], "active_node_id": before["activeNodeId"], "moves": ["e7e5"]})["isError"]
            client.post(URL + f'/0/attempts/{attempt["id"]}/interrupt')
        finally:
            runtime.release.set()
        events(client, attempt["eventsUrl"])
        runtime.ready.clear()
        runtime.release.clear()
        retry = client.post(URL + f'/0/attempts/{attempt["id"]}/retry')
        assert retry.status_code == 202
        try:
            assert runtime.ready.wait(5)
            assert runtime.context["active_position"] == original_fen
            assert runtime.context["response_request"]["body"] == "Why this position?"
            saved = client.get(URL + "/0").json()
            assert len(saved["nodes"]) == 2
            assert len([m for m in saved["messages"] if m["author"] == "player"]) == 1
        finally:
            runtime.release.set()
        events(client, retry.json()["eventsUrl"])
