"""Disposable runtime context. All delivery state is derived from SQLite history."""

import json
from typing import cast

from trainer.application.ports import LocalStorage, SessionCheckpoint


CONTEXT_BUDGET = 24_000
RECENT_MESSAGES = 6


def context_size(context: dict[str, object]) -> int:
    # Game, Analysis and the Variation tree must always remain available in full;
    # the budget applies to the disposable dialogue, not immutable chess records.
    return len(json.dumps({key: context[key] for key in (
        "history", "response_attempts", "derived_summary", "practical_line",
    ) if key in context}, ensure_ascii=False))


def compact_context(context: dict[str, object]) -> dict[str, object]:
    history = cast(list[dict[str, str]], context["history"])
    if len(history) <= RECENT_MESSAGES or context_size(context) <= CONTEXT_BUDGET:
        return context
    older = history[:-RECENT_MESSAGES]
    attempts = cast(list[dict[str, object]], context["response_attempts"])
    # A deterministic extractive synopsis is cheap and regenerable. It is not a
    # visible entry, a source of chess evidence, or a substitute for MCP reads.
    excerpts = [f"{m['author']}: {m['body'][:200]}" for m in older]
    outcomes = [
        f"Attempt {a['id']}: {a['status']}; " + "; ".join(
            f"{e['type']}: {str(e.get('text', ''))[:300]}"
            for e in cast(list[dict[str, object]], a["events"])
            if e["type"] in {"evidence", "failed", "interrupted", "limited"}
        ) for a in attempts[:-3]
    ]
    return {**context, "history": history[-RECENT_MESSAGES:],
            "response_attempts": attempts[-3:], "derived_summary": {
                "source": "persisted_discussion", "authoritative": False,
                "method": "extractive-v1", "messages_covered": len(older),
                "text": "\n".join(excerpts + outcomes)[:8000],
                "notice": "Lossy derived context. Use trainer_read_context for full original history and evidence.",
            }}


def checkpoint(storage: LocalStorage, key: tuple[str, str, int], chars: int) -> SessionCheckpoint:
    discussion = next(d for d in storage.list_discussions(*key[:2]) if d.base_ply == key[2])
    return SessionCheckpoint(
        history=len(discussion.messages), nodes=len(discussion.nodes),
        events={str(a.id): len(a.events) for a in storage.response_attempts(*key)},
        context_chars=chars,
    )


def delta_context(context: dict[str, object], previous: SessionCheckpoint) -> dict[str, object]:
    result = {key: context[key] for key in (
        "base_ply", "revision", "active_node_id", "active_position",
    )}
    result["context_delivery"] = "delta"
    result["history"] = cast(list[object], context["history"])[previous.history:]
    result["variations"] = cast(list[object], context["variations"])[previous.nodes:]
    attempts = cast(list[dict[str, object]], context["response_attempts"])
    result["response_attempts"] = [
        {**a, "events": cast(list[object], a["events"])[previous.events.get(str(a["id"]), 0):]}
        for a in attempts if len(cast(list[object], a["events"])) > previous.events.get(str(a["id"]), 0)
    ]
    for key in ("attempt_id", "response_request", "practical_line"):
        if key in context:
            result[key] = context[key]
    return result
