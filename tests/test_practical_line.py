from collections.abc import Callable
import json
from typing import Any

from fastapi.testclient import TestClient
import pytest

from lichess_mock import LichessMock
from test_trainer_evidence import EvidenceEvaluator
from test_trainer_mcp import ToolOpenCode, call
from test_trainer_stream import URL, events
from test_analysis import PositionOnlyEvaluator, FailingCandidateEvaluator
from trainer.bootstrap import create_app
from trainer.application.ports import TrainerEvent
from trainer.config import Settings
from trainer.domain import EvaluationRun, EvaluationScore, PositionEvaluation


class PracticalEvaluator(EvidenceEvaluator):
    def evaluate_candidates(self, position: str, max_candidates: int) -> EvaluationRun:
        run = super().evaluate_candidates(position, max_candidates)
        return EvaluationRun(run.provenance, [
            PositionEvaluation(position, EvaluationScore("cp", score), e.best_move, e.principal_variation)
            for score, e in zip([45, 30, 15, 0], run.evaluations)
        ])


class PracticalOpenCode(ToolOpenCode):
    def stream(self, session_id: str, system: str, context: str, control: Any = None) -> Any:
        self.context = json.loads(context)
        self.system = system
        self.ready.set()
        assert self.release.wait(10)
        yield TrainerEvent("text", "The demonstrated move offers a clear development plan with fewer difficult decisions. ")
        yield TrainerEvent("text", "Compare its Player score with the Best Line card; without an Evaluator this judgment is unverified.")
        yield TrainerEvent("completed")


@pytest.mark.parametrize("candidate", [0, 2])
def test_explicit_practical_line_creates_demonstrates_and_preserves_tradeoff(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock, candidate: int,
) -> None:
    settings = migrated_settings("Lance5500")
    runtime = PracticalOpenCode()
    evaluator = PracticalEvaluator()
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime, candidate_evaluator=evaluator)) as client:
        client.post("/sync")
        preview = client.post("/analyses/lichess/q7ZvsdUF/best-line", json={"ply": 0}).json()
        assert not runtime.ready.is_set()
        assert client.get(URL + "/0").status_code == 404
        client.post("/trainer/consent", json={"accepted": True})
        response = client.post("/analyses/lichess/q7ZvsdUF/practical-line", json={"ply": 0})
        assert response.status_code == 202
        attempt = response.json()
        assert runtime.ready.wait(5)
        try:
            request = runtime.context["practical_line"]
            assert request["evidence"]["status"] == "verified"
            assert len(request["evidence"]["candidates"]) == 3
            assert "clarity" in runtime.system and "practical chances" in runtime.system
            assert "player_rating" in runtime.context
            line = request["evidence"]["candidates"][candidate]["moves"]
            context = call(client, runtime.token, "read_context", {"attempt_id": attempt["id"]})["structuredContent"]
            result = call(client, runtime.token, "play_line", {
                "attempt_id": attempt["id"], "revision": context["revision"],
                "active_node_id": context["active_node_id"], "moves": [m["uci"] for m in line],
            })
            assert not result["isError"]
        finally:
            runtime.release.set()
        output = events(client, attempt["eventsUrl"])
        assert output[-1]["type"] == "completed"
        saved = client.get(URL + "/0").json()
        assert saved["nodes"][0]["author"] == "trainer"
        assert saved["nodes"][0]["uci"] == line[0]["uci"]
        assert "clear development plan" in saved["messages"][-1]["body"]
        comparison = saved["practicalLines"][0]
        assert comparison["status"] == "verified"
        assert comparison["bestScore"] == preview["score"]
        assert comparison["selectedScore"] == {"kind": "cp", "value": 45 if candidate == 0 else 15}
        assert comparison["differenceCp"] == (0 if candidate == 0 else -30)
        assert comparison["isBestLine"] == (candidate == 0)
        assert 'data-action="practical-line"' in client.get("/analyses/lichess/q7ZvsdUF").text
        assert 'class="practical-line-evidence"' in client.get("/analyses/lichess/q7ZvsdUF").text
        assert f'Difference (cp, Player): {0 if candidate == 0 else -30}' in client.get("/analyses/lichess/q7ZvsdUF").text
    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"))) as client:
        assert client.get(URL + "/0").json() == saved


@pytest.mark.parametrize("target", ["game", "variation", "preview", "preview-zero", "exploration"])
def test_practical_line_targets_active_position_and_resumes_one_discussion(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock, target: str,
) -> None:
    runtime = PracticalOpenCode()
    evaluator = PracticalEvaluator()
    with TestClient(create_app(migrated_settings("Lance5500"), lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime, candidate_evaluator=evaluator)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        body: dict[str, Any] = {"ply": 0}
        if target == "exploration":
            body["initial_moves"] = ["e2e4"]
        elif target == "variation":
            first = client.post(URL, data={"base_ply": "0", "message": "Explore", "initial_moves": '["e2e4"]',
                                          "stream": "true"}).json()
            runtime.release.set()
            events(client, first["eventsUrl"])
            runtime.ready.clear()
            runtime.release.clear()
            before = client.get(URL + "/0").json()
            body.update(revision=before["revision"], position={"kind": "variation", "node_id": before["activeNodeId"]})
        elif target.startswith("preview"):
            preview = client.post("/analyses/lichess/q7ZvsdUF/best-line", json={"ply": 0}).json()
            body["position"] = {"kind": "preview", "preview_id": preview["previewId"],
                                "ply": 0 if target == "preview-zero" else 1}
        response = client.post("/analyses/lichess/q7ZvsdUF/practical-line", json=body)
        assert response.status_code == 202
        attempt = response.json()
        assert runtime.ready.wait(5)
        try:
            saved = client.get(URL + "/0").json()
            expected_fen = saved["activeFen"]
            assert runtime.context["active_position"] == expected_fen
            assert evaluator.requests[-1] == (expected_fen, 3)
            assert len(saved["nodes"]) == (1 if target in {"variation", "preview", "exploration"} else 0)
            if target == "preview":
                assert saved["nodes"][0]["author"] == "evaluator"
            candidate = runtime.context["practical_line"]["evidence"]["candidates"][1]
            assert not call(client, runtime.token, "play_line", {
                "attempt_id": attempt["id"], "revision": saved["revision"], "active_node_id": saved["activeNodeId"],
                "moves": [m["uci"] for m in candidate["moves"]],
            })["isError"]
        finally:
            runtime.release.set()
        assert events(client, attempt["eventsUrl"])[-1]["type"] == "completed"
        before = client.get(URL + "/0").json()
        runtime.ready.clear()
        runtime.release.clear()
        # Game mode explicitly targets the Base Position, even when a Variation was active.
        response = client.post("/analyses/lichess/q7ZvsdUF/practical-line", json={"ply": 0, "revision": before["revision"]})
        assert response.status_code == 202
        resumed = response.json()
        assert runtime.ready.wait(5)
        try:
            context = runtime.context
            assert context["active_node_id"] is None
            assert context["active_position"] == before["baseFen"]
            assert context["context_delivery"] == "delta"
            assert context["variations"] == []
            assert context["practical_line"]["evidence"]["status"] == "verified"
            refreshed = call(client, runtime.token, "read_context", {"attempt_id": resumed["id"]})["structuredContent"]
            assert [n["san"] for n in refreshed["variations"]] == [n["san"] for n in before["nodes"]]
            assert client.get(URL + "/0").json()["revision"] > before["revision"]
        finally:
            runtime.release.set()
        # A runtime that omits play_line cannot complete a Practical Line successfully.
        assert events(client, resumed["eventsUrl"])[-1]["type"] == "failed"


@pytest.mark.parametrize("failure", ["missing", "failed"])
def test_practical_line_without_evaluator_demonstrates_unverified_chess_knowledge(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock, failure: str,
) -> None:
    runtime = PracticalOpenCode()
    with TestClient(create_app(migrated_settings("Lance5500"), lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime, position_evaluator=PositionOnlyEvaluator(),
                              candidate_evaluator=FailingCandidateEvaluator() if failure == "failed" else None)) as client:
        client.post("/sync")
        assert client.post("/analyses/lichess/q7ZvsdUF/best-line", json={"ply": 0}).status_code == 503
        assert client.post("/analyses/lichess/q7ZvsdUF/practical-line", json={"ply": 0}).status_code == 403
        assert client.get(URL + "/0").status_code == 404
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post("/analyses/lichess/q7ZvsdUF/practical-line", json={"ply": 0}).json()
        assert runtime.ready.wait(5)
        try:
            assert runtime.context["practical_line"]["evidence"]["status"] == "unavailable"
            before = client.get(URL + "/0").json()
            assert not call(client, runtime.token, "play_line", {
                "attempt_id": attempt["id"], "revision": before["revision"], "active_node_id": None,
                "moves": ["e2e4", "e7e5"],
            })["isError"]
        finally:
            runtime.release.set()
        assert events(client, attempt["eventsUrl"])[-1]["type"] == "completed"
        line = client.get(URL + "/0").json()["practicalLines"][0]
        assert line["status"] == "unverified"
        assert line["bestScore"] is None and line["selectedScore"] is None
        assert line["differenceCp"] is None
        assert "unverified" in client.get("/analyses/lichess/q7ZvsdUF").text


def test_practical_line_preserves_atomicity_limits_and_retry_request(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    runtime = PracticalOpenCode()
    evaluator = PracticalEvaluator()
    with TestClient(create_app(migrated_settings("Lance5500"), lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime, candidate_evaluator=evaluator)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        endpoint = "/analyses/lichess/q7ZvsdUF/practical-line"
        for bad in [{"ply": 9999}, {"ply": 0, "initial_moves": ["e2e5"]},
                    {"ply": 0, "position": {"kind": "game", "ply": 1}},
                    {"ply": 0, "position": {"kind": "preview", "preview_id": "forged", "ply": 0}}]:
            assert client.post(endpoint, json=bad).status_code == 422
            assert client.get(URL + "/0").status_code == 404
        # Non-zero Game ply is the Base Position identity, not merely an isolated FEN.
        attempt = client.post(endpoint, json={"ply": 2}).json()
        assert runtime.ready.wait(5)
        try:
            before = client.get(URL + "/2").json()
            assert runtime.context["base_ply"] == 2
            assert runtime.context["active_position"] == before["baseFen"]
            assert runtime.context["practical_line"]["evidence"]["context"] == {
                "position": "Game · Position after 1... d5", "line": "1. d4 1... d5",
            }
            assert len(evaluator.requests) == 1
            assert client.post(endpoint, json={"ply": 2, "revision": before["revision"]}).status_code == 409
            for moves in [["e2e4", "e7e6", "e4e6"], ["e2e4"] * 13]:
                assert call(client, runtime.token, "play_line", {
                    "attempt_id": attempt["id"], "revision": before["revision"], "active_node_id": None, "moves": moves,
                })["isError"]
                assert client.get(URL + "/2").json() == before
            for _ in range(2):
                assert not call(client, runtime.token, "evaluate_position", {
                    "attempt_id": attempt["id"], "position": {"kind": "game", "ply": 2},
                })["isError"]
            assert call(client, runtime.token, "evaluate_position", {
                "attempt_id": attempt["id"], "position": {"kind": "game", "ply": 2},
            })["isError"]
        finally:
            runtime.release.set()
        assert events(client, attempt["eventsUrl"])[-1]["type"] == "limited"
        assert len(evaluator.requests) == 3
        before = client.get(URL + "/2").json()
        assert client.post(endpoint, json={"ply": 2, "revision": before["revision"] - 1}).status_code == 409
        assert client.get(URL + "/2").json() == before
        runtime.ready.clear()
        runtime.release.clear()
        retry = client.post(URL + f'/2/attempts/{attempt["id"]}/retry').json()
        assert runtime.ready.wait(5)
        try:
            assert runtime.context["practical_line"]["evidence"]["status"] == "verified"
            assert len(evaluator.requests) == 4
            saved = client.get(URL + "/2").json()
            assert [m for m in saved["messages"] if m["author"] == "player"] == [
                {"author": "player", "body": "Mostrar linha prática"},
            ]
            assert not call(client, runtime.token, "play_line", {
                "attempt_id": retry["id"], "revision": saved["revision"], "active_node_id": None,
                "moves": ["e2e4"],
            })["isError"]
        finally:
            runtime.release.set()
        assert events(client, retry["eventsUrl"])[-1]["type"] == "completed"
        comparison = client.get(URL + "/2").json()["practicalLines"][0]
        # A legal but unscored move cannot inherit a candidate's authoritative score.
        assert comparison["status"] == "unverified"
        assert comparison["selectedScore"] is None
