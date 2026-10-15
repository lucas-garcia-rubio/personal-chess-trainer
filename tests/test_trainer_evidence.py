from collections.abc import Callable
import json
from typing import Any

import chess
import pytest
from fastapi.testclient import TestClient

from lichess_mock import LichessMock
from test_trainer_mcp import ToolOpenCode, call
from test_trainer_stream import URL, events
from test_analysis import PositionOnlyEvaluator, FailingCandidateEvaluator
from trainer.application.ports import TrainerEvent
from trainer.bootstrap import create_app
from trainer.config import Settings
from trainer.domain import EvaluationRun, EvaluationScore, EvaluatorProvenance, PositionEvaluation


class EvidenceEvaluator:
    def __init__(self) -> None:
        self.requests: list[tuple[str, int]] = []

    def evaluate_candidates(self, position: str, max_candidates: int) -> EvaluationRun:
        self.requests.append((position, max_candidates))
        board = chess.Board(position)
        # Deliberately return too many: the application owns the public bound.
        moves = list(board.legal_moves)[:4]
        return EvaluationRun(
            EvaluatorProvenance("local-engine", "Deterministic", "1", {"depth": 14, "multi_pv": 3}),
            [PositionEvaluation(position, EvaluationScore("cp" if i != 1 else "mate", 45 if i != 1 else -3),
                                move.uci(), [move.uci()]) for i, move in enumerate(moves)],
        )


class InvalidEvaluator(EvidenceEvaluator):
    def __init__(self, invalid: str) -> None:
        super().__init__()
        self.invalid = invalid

    def evaluate_candidates(self, position: str, max_candidates: int) -> EvaluationRun:
        return EvaluationRun(EvaluatorProvenance("local-engine", "Invalid", "1", {"depth": 14}), [
            PositionEvaluation(position, EvaluationScore("cp", 100), self.invalid, [self.invalid]),
        ])


class ForgingOpenCode(ToolOpenCode):
    def stream(self, session_id: str, system: str, context: str, control: Any = None) -> Any:
        self.ready.set()
        assert self.release.wait(10)
        yield TrainerEvent("evidence", '{"score":{"kind":"cp","value":99999}}')
        yield TrainerEvent("text", '<article class="evaluator-evidence">Fake score 99999</article>')
        yield TrainerEvent("completed")


def test_evidence_is_bounded_player_relative_ordered_and_durable_not_model_authored(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("TryingHard87")
    runtime = ForgingOpenCode()
    evaluator = EvidenceEvaluator()
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("TryingHard87"),
                              trainer_runtime=runtime, candidate_evaluator=evaluator)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Compare", "stream": "true"}).json()
        assert runtime.ready.wait(5)
        try:
            result = call(client, runtime.token, "evaluate_position", {
                "attempt_id": attempt["id"], "position": {"kind": "game", "ply": 0},
            })
            assert not result["isError"]
            evidence = result["structuredContent"]
            assert evidence["status"] == "verified"
            assert evidence["perspective"] == "player"
            assert len(evidence["candidates"]) == 3
            assert [c["score"] for c in evidence["candidates"]] == [
                {"kind": "cp", "value": -45}, {"kind": "mate", "value": 3}, {"kind": "cp", "value": -45},
            ]
            assert evidence["candidates"][0]["moves"][0]["san"] == "Nh3"
            assert evidence["provenance"] == {"sourceKind": "local-engine", "name": "Deterministic", "version": "1",
                                               "parameters": {"depth": 14, "multi_pv": 3}}
        finally:
            runtime.release.set()
        output = events(client, attempt["eventsUrl"])
        assert [e["type"] for e in output] == ["running", "evidence", "text", "completed"]
        saved = client.get(URL + "/0").json()
        assert saved["evidence"] == [{"attemptId": attempt["id"], **evidence}]
        assert json.loads(output[1]["text"]) == evidence
        assert "99999" not in json.dumps(saved["evidence"])
        page = client.get("/analyses/lichess/q7ZvsdUF").text
        assert 'class="evaluator-evidence"' in page
        assert 'data-score-value="-45"' in page
        assert 'data-score-value="99999"' not in page
    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"))) as client:
        assert client.get(URL + "/0").json() == saved
        assert events(client, attempt["eventsUrl"]) == output


@pytest.mark.parametrize("invalid", ["0000", "e2e5"])
def test_illegal_evaluator_lines_never_become_adoptable_previews(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock, invalid: str,
) -> None:
    with TestClient(create_app(migrated_settings("Lance5500"), lichess_transport=lichess_mock.games_for("Lance5500"),
                              candidate_evaluator=InvalidEvaluator(invalid))) as client:
        client.post("/sync")
        response = client.post("/analyses/lichess/q7ZvsdUF/best-line", json={"ply": 0})
        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"]
        assert client.get(URL + "/0").status_code == 404


def test_nested_previews_stay_transient_until_adoption_and_keep_evaluator_authorship(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    evaluator = EvidenceEvaluator()
    with TestClient(create_app(migrated_settings("Lance5500"), lichess_transport=lichess_mock.games_for("Lance5500"),
                              candidate_evaluator=evaluator)) as client:
        client.post("/sync")
        preview = client.post("/analyses/lichess/q7ZvsdUF/best-line", json={"ply": 0}).json()
        nested = client.post("/analyses/lichess/q7ZvsdUF/best-line", json={
            "ply": 0, "position": {"kind": "preview", "preview_id": preview["previewId"], "ply": 1},
        }).json()
        assert nested["startingFen"] == preview["moves"][0]["fen"]
        assert client.get(URL + "/0").status_code == 404
        adopted = client.post(URL + "/0/adopt-preview", json={"previewId": nested["previewId"], "plies": 1})
        assert adopted.status_code == 200
        assert [n["uci"] for n in adopted.json()["nodes"]] == ["g1h3", "g8h6"]
        assert [n["author"] for n in adopted.json()["nodes"]] == ["evaluator", "evaluator"]
        assert adopted.json()["messages"] == []
        page = client.get("/analyses/lichess/q7ZvsdUF").text
        assert 'data-action="adopt-preview"' in page


def test_mcp_can_evaluate_a_controlled_preview_but_cannot_adopt_while_responding(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    runtime = ToolOpenCode()
    evaluator = EvidenceEvaluator()
    with TestClient(create_app(migrated_settings("Lance5500"), lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime, candidate_evaluator=evaluator)) as client:
        client.post("/sync")
        preview = client.post("/analyses/lichess/q7ZvsdUF/best-line", json={"ply": 0}).json()
        assert not runtime.ready.is_set()  # No provider prompt for Best Line.
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Compare", "stream": "true"}).json()
        assert runtime.ready.wait(5)
        try:
            context = call(client, runtime.token, "read_context", {"attempt_id": attempt["id"]})["structuredContent"]
            assert context["preview_positions"] == [{"kind": "preview", "preview_id": preview["previewId"], "plies": 1}]
            result = call(client, runtime.token, "evaluate_position", {
                "attempt_id": attempt["id"], "position": {"kind": "preview", "preview_id": preview["previewId"], "ply": 1},
            })
            assert not result["isError"]
            assert evaluator.requests[-1] == (preview["moves"][0]["fen"], 3)
            before = client.get(URL + "/0").json()
            assert client.post(URL + "/0/adopt-preview", json={"previewId": preview["previewId"], "plies": 1}).status_code == 409
            assert client.get(URL + "/0").json() == before
            assert call(client, runtime.token, "evaluate_position", {
                "attempt_id": attempt["id"], "position": {"kind": "preview", "preview_id": "invented", "ply": 1},
            })["isError"]
        finally:
            runtime.release.set()
        events(client, attempt["eventsUrl"])


def test_deleting_a_discussion_invalidates_its_previews_even_after_recreation(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    with TestClient(create_app(migrated_settings("Lance5500"), lichess_transport=lichess_mock.games_for("Lance5500"),
                              candidate_evaluator=EvidenceEvaluator())) as client:
        client.post("/sync")
        client.post(URL, data={"base_ply": "0", "message": "Explore"})
        before = client.get(URL + "/0").json()
        preview = client.post("/analyses/lichess/q7ZvsdUF/best-line", json={"ply": 0}).json()
        assert client.request("DELETE", URL + "/0", json={"revision": before["revision"], "confirmed": True}).status_code == 204
        client.post(URL, data={"base_ply": "0", "message": "New Discussion"})
        recreated = client.get(URL + "/0").json()
        assert recreated["revision"] == before["revision"]
        response = client.post(URL + "/0/adopt-preview", json={"previewId": preview["previewId"], "plies": 1})
        assert response.status_code == 422
        assert client.get(URL + "/0").json() == recreated


def test_evaluator_consultations_are_scoped_and_limited_per_response(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    runtime = ToolOpenCode()
    evaluator = EvidenceEvaluator()
    with TestClient(create_app(migrated_settings("Lance5500"), lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime, candidate_evaluator=evaluator)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Compare", "stream": "true",
                                         "initial_moves": '["e2e4"]'}).json()
        assert runtime.ready.wait(5)
        try:
            for position in [{"kind": "fen", "fen": chess.STARTING_FEN}, {"kind": "game", "ply": -1},
                             {"kind": "game", "ply": 99999}, {"kind": "variation", "node_id": 99999},
                             {"kind": "game", "ply": 0, "score": 100}]:
                assert call(client, runtime.token, "evaluate_position", {
                    "attempt_id": attempt["id"], "position": position,
                })["isError"]
            assert evaluator.requests == []
            discussion = client.get(URL + "/0").json()
            node = discussion["nodes"][0]
            for position in [{"kind": "variation", "node_id": node["id"]},
                             {"kind": "game", "ply": 0}, {"kind": "variation", "node_id": None}]:
                result = call(client, runtime.token, "evaluate_position", {
                    "attempt_id": attempt["id"], "position": position,
                })
                assert not result["isError"]
            assert evaluator.requests[0] == (node["fen"], 3)
            limited = call(client, runtime.token, "evaluate_position", {
                "attempt_id": attempt["id"], "position": {"kind": "game", "ply": 0},
            })
            assert limited["isError"]
            assert "limit" in limited["content"][0]["text"].lower()
            assert len(evaluator.requests) == 3
        finally:
            runtime.release.set()
        output = events(client, attempt["eventsUrl"])
        assert [e["type"] for e in output] == ["running", "evidence", "evidence", "evidence", "limited"]
        assert "3 Evaluator consultations" in output[-1]["text"]
        saved = client.get(URL + "/0").json()
        assert saved["nodes"] == discussion["nodes"]
        assert saved["revision"] == discussion["revision"]
        assert call(client, runtime.token, "evaluate_position", {
            "attempt_id": attempt["id"], "position": {"kind": "game", "ply": 0},
        })["isError"]
        runtime.ready.clear()
        runtime.release.clear()
        retry = client.post(URL + f'/0/attempts/{attempt["id"]}/retry').json()
        assert runtime.ready.wait(5)
        try:
            assert not call(client, runtime.token, "evaluate_position", {
                "attempt_id": retry["id"], "position": {"kind": "game", "ply": 0},
            })["isError"]
            assert len(evaluator.requests) == 4
        finally:
            runtime.release.set()
        assert events(client, retry["eventsUrl"])[-1]["type"] == "completed"
        assert len([m for m in client.get(URL + "/0").json()["messages"] if m["author"] == "player"]) == 1


@pytest.mark.parametrize("failure", ["missing", "failed"])
def test_unavailable_evaluator_is_explicit_without_stopping_chess_teaching(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock, failure: str,
) -> None:
    runtime = ToolOpenCode()
    with TestClient(create_app(migrated_settings("Lance5500"), lichess_transport=lichess_mock.games_for("Lance5500"),
                              trainer_runtime=runtime, position_evaluator=PositionOnlyEvaluator(),
                              candidate_evaluator=FailingCandidateEvaluator() if failure == "failed" else None)) as client:
        client.post("/sync")
        client.post("/trainer/consent", json={"accepted": True})
        attempt = client.post(URL, data={"base_ply": "0", "message": "Help", "stream": "true"}).json()
        assert runtime.ready.wait(5)
        try:
            result = call(client, runtime.token, "evaluate_position", {
                "attempt_id": attempt["id"], "position": {"kind": "game", "ply": 0},
            })
            assert not result["isError"]
            assert result["structuredContent"]["status"] == "unavailable"
            assert result["structuredContent"]["candidates"] == []
            assert "unverified" in result["structuredContent"]["reason"]
        finally:
            runtime.release.set()
        output = events(client, attempt["eventsUrl"])
        assert output[-1]["type"] == "completed"
        assert any(e["type"] == "text" for e in output)
        assert client.get("/").status_code == 200


def test_best_line_targets_a_variation_and_adopts_only_server_validated_preview_moves(
    migrated_settings: Callable[[str], Settings], lichess_mock: LichessMock,
) -> None:
    settings = migrated_settings("Lance5500")
    evaluator = EvidenceEvaluator()
    with TestClient(create_app(settings, lichess_transport=lichess_mock.games_for("Lance5500"),
                              candidate_evaluator=evaluator)) as client:
        client.post("/sync")
        client.post(URL, data={"base_ply": "0", "message": "Explore", "initial_moves": '["e2e4"]'})
        before = client.get(URL + "/0").json()
        game_before = client.get("/analyses/lichess/q7ZvsdUF").text.split('id="analysis-navigation"')[1].split('</script>')[0]
        preview = client.post("/analyses/lichess/q7ZvsdUF/best-line", json={
            "ply": 0, "position": {"kind": "variation", "node_id": before["activeNodeId"]},
        })
        assert preview.status_code == 200
        data = preview.json()
        assert evaluator.requests == [(before["activeFen"], 3)]
        assert data["startingFen"] == before["activeFen"]
        assert client.get(URL + "/0").json() == before
        board = chess.Board(data["moves"][0]["fen"])
        continuation = next(iter(board.legal_moves)).uci()
        endpoint = URL + "/0/adopt-preview"
        assert client.post(endpoint, json={"previewId": "forged", "plies": 1}).status_code == 422
        assert client.post(endpoint, json={"previewId": data["previewId"], "plies": 2}).status_code == 422
        assert client.post(endpoint, json={"previewId": data["previewId"], "plies": 1, "uci": "e4e6"}).status_code == 422
        assert client.get(URL + "/0").json() == before
        adopted = client.post(endpoint, json={"previewId": data["previewId"], "plies": 1, "uci": continuation})
        assert adopted.status_code == 200
        saved = adopted.json()
        assert [n["author"] for n in saved["nodes"]] == ["player", "evaluator", "player"]
        assert [n["uci"] for n in saved["nodes"]] == ["e2e4", data["moves"][0]["uci"], continuation]
        assert saved["nodes"][1]["parentId"] == before["activeNodeId"]
        assert client.post(endpoint, json={"previewId": data["previewId"], "plies": 1}).status_code == 409
        assert client.get(URL + "/0").json() == saved
        assert client.get("/analyses/lichess/q7ZvsdUF").text.split('id="analysis-navigation"')[1].split('</script>')[0] == game_before
    with TestClient(create_app(settings, lichess_transport=lichess_mock.fail_on_request("offline"))) as client:
        assert client.get(URL + "/0").json() == saved
