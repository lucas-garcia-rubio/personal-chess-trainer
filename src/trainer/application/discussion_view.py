"""Replay the visible log up to a specific response cursor, never a future snapshot."""
import json
from typing import Any, cast

from trainer.application.ports import ResponseAttempt


def discussion_view(document: dict[str, Any], requests: list[tuple[int, str]],
                    attempts: list[ResponseAttempt], attempt_id: int | None = None,
                    cursor: int | None = None) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    emitted: set[int] = set()
    board = dict(document)
    for field in ("messages", "evidence", "practicalLines"):
        board.pop(field, None)
    visible_attempts = []
    for attempt in attempts:
        if attempt_id is not None and attempt.id > attempt_id:
            break
        for identifier, body in requests:
            if identifier <= attempt.request_id and identifier not in emitted:
                entries.append({"type": "player", "text": body})
                emitted.add(identifier)
        events = [event for event in attempt.events
                  if attempt.id != attempt_id or cursor is None or cast(int, event["sequence"]) <= cursor]
        status = "running"
        if attempt.id == attempt_id:
            # The running event records the exact starting tree/selection. Later
            # requests and already-produced events cannot leak into this replay.
            snapshot_text = str(attempt.events[0]["text"])
            if snapshot_text:
                snapshot = json.loads(snapshot_text)
                board.update(baseFen=snapshot["base_fen"], revision=snapshot["revision"],
                             activeNodeId=snapshot["active_node_id"],
                             nodes=[{"id": n["id"], "parentId": n["parent_id"], "author": n["author"],
                                     "uci": n["uci"], "san": n["san"], "fen": n["resulting_fen"]}
                                    for n in snapshot["nodes"]])
            else:
                # Legacy attempts have no starting snapshot. Only the ancestors
                # of their first demonstrated move are provably pre-cursor.
                first_move = next((json.loads(str(e["text"])) for e in attempt.events
                                   if e["type"] == "move"), None)
                anchor = first_move["parentId"] if first_move else None
                ancestors = []
                parent = anchor
                while parent is not None:
                    node = next(n for n in board["nodes"] if n["id"] == parent)
                    ancestors.append(node)
                    parent = node["parentId"]
                board["nodes"] = list(reversed(ancestors))
                board["activeNodeId"] = anchor
                board["revision"] = attempt.revision
        response: dict[str, Any] = {"type": "response", "attemptId": attempt.id,
                                    "retry": attempt.retry_of is not None, "entries": []}
        entries.append(response)
        blocks = response["entries"]
        for event in events:
            kind = event["type"]
            if kind in {"completed", "failed", "interrupted", "limited"}:
                status = str(kind)
                continue
            if kind in {"running", "activity"}:
                continue
            entry: dict[str, Any] = {**event, "attemptId": attempt.id}
            if kind in {"move", "evidence", "practical_line"}:
                entry["value"] = json.loads(str(event["text"]))
            # Runtime text events are deltas, not separate Trainer messages.
            # Merge adjacent visible text only within the same attempt; moves
            # and evidence must retain their place between text blocks.
            if (kind == "text" and blocks and blocks[-1]["type"] == "text"
                    and blocks[-1].get("attemptId") == attempt.id):
                blocks[-1]["text"] += str(event["text"])
            elif kind == "move":
                if (blocks and blocks[-1]["type"] == "line"
                        and blocks[-1]["moves"][-1]["id"] == entry["value"]["parentId"]):
                    blocks[-1]["moves"].append(entry["value"])
                else:
                    blocks.append({"type": "line", "moves": [entry["value"]]})
            else:
                blocks.append(entry)
            if attempt.id == attempt_id:
                if kind == "move":
                    node = entry["value"]
                    board["nodes"].append({**node, "fen": node["resultingFen"]})
                    board["activeNodeId"] = node["id"]
                    board["revision"] = node["revision"]
                elif kind == "text":
                    board["revision"] += 1
        response["status"] = status
        visible_attempts.append({"id": attempt.id, "status": status, "retry_of": attempt.retry_of,
                                 "events": events})
    if attempt_id is None:
        for identifier, body in requests:
            if identifier not in emitted:
                entries.append({"type": "player", "text": body})
    path: list[dict[str, Any]] = []
    node_id = board["activeNodeId"]
    while node_id is not None:
        node = next(n for n in board["nodes"] if n["id"] == node_id)
        path.insert(0, node)
        node_id = node["parentId"]
    board.update(moves=path, activeFen=path[-1]["fen"] if path else board["baseFen"], attempts=visible_attempts)
    nodes = {node["id"]: node for node in board["nodes"]}

    def labeled(node: dict[str, Any]) -> dict[str, Any]:
        parent = nodes.get(node["parentId"])
        fen = parent["fen"] if parent else board["baseFen"]
        fields = fen.split()
        prefix = fields[5] + ("." if fields[1] == "w" else "...")
        return {**node, "label": f"{prefix} {node['san']}"}

    for item in entries:
        if item["type"] == "response":
            for block in item["entries"]:
                if block["type"] == "line":
                    block["moves"] = [labeled(node) for node in block["moves"]]
    parents = {node["parentId"] for node in nodes.values()}
    variations = []
    for node in nodes.values():
        if node["id"] not in parents:
            line: list[dict[str, Any]] = []
            current: dict[str, Any] | None = node
            while current is not None:
                line.insert(0, labeled(current))
                current = nodes.get(current["parentId"])
            variations.append(line)
    return {"variations": variations, "entries": entries, "board": board, "attempt_id": attempt_id, "cursor": cursor}
