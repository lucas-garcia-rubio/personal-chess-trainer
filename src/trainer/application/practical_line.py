"""Explicit direct teaching request, with application-owned score comparisons."""
import json
from typing import cast

from trainer.application.ports import LocalStorage, TrainerEvent


PRACTICAL_LINE_REQUEST = "Mostrar linha prática"


def record_practical_line(storage: LocalStorage, key: tuple[str, str, int],
                          attempt_id: int, starting_fen: str, moves: list[str]) -> None:
    attempt = next(a for a in storage.response_attempts(*key) if a.id == attempt_id)
    evidence = next((json.loads(cast(str, e["text"])) for e in attempt.events
                     if e["type"] == "evidence" and json.loads(cast(str, e["text"])).get("purpose") == "practical_line"), None)
    if evidence is None:
        return
    candidates = evidence["candidates"]
    # A different position or an unscored move must never borrow a candidate's score.
    selected = next((c for c in candidates if c["moves"] and c["moves"][0]["uci"] == moves[0]), None)
    verified = evidence["position"] == starting_fen and selected is not None
    best_score = candidates[0]["score"] if candidates and evidence["position"] == starting_fen else None
    selected_score = selected["score"] if verified and selected is not None else None
    comparison = {
        "status": "verified" if verified else "unverified",
        "reason": "Evaluator candidate score; continuation is Trainer judgment." if verified else
                  "Practical Line unverified: no Evaluator score for this recommendation.",
        "position": starting_fen, "moves": moves,
        "bestScore": best_score, "selectedScore": selected_score,
        "differenceCp": (selected_score["value"] - best_score["value"]
                         if selected_score and best_score and selected_score["kind"] == best_score["kind"] == "cp" else None),
        "isBestLine": verified and selected is candidates[0],
    }
    storage.record_response_event(attempt_id, TrainerEvent("practical_line", json.dumps(comparison)))
