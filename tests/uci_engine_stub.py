#!/usr/bin/env python3
"""Scriptable UCI engine double for the Stockfish adapter tests.

Started as the engine executable, it replays the responses scripted in the JSON
file at ``$UCI_ENGINE_SCENARIO`` and appends every command it receives — plus a
``start`` marker per boot — to the log at ``$UCI_ENGINE_LOG``. That lets the
suite verify the adapter's UCI conversation without Stockfish installed.

Scenario shape::

    {
      "name": "StubEngine 2.3",
      "positions": {"<fen>": ["info depth 15 ... pv e2e4", "bestmove e2e4"]},
      "exit_on": "uci"
    }

``exit_on`` makes the stub exit right after receiving a command starting with
that prefix, to simulate an engine that dies mid-conversation. ``blank_lines``
makes the stub emit empty lines in its ``uci`` reply, as Stockfish 19 does
between the ``id`` lines and ``uciok``.
"""

import json
import os
from pathlib import Path
import sys


def main() -> None:
    scenario = json.loads(
        Path(os.environ["UCI_ENGINE_SCENARIO"]).read_text(encoding="utf-8")
    )
    name = str(scenario.get("name", "StubEngine 1.0"))
    positions: dict[str, list[str]] = {
        str(fen): [str(line) for line in lines]
        for fen, lines in scenario.get("positions", {}).items()
    }
    exit_prefix = scenario.get("exit_on")
    exit_on = str(exit_prefix) if exit_prefix is not None else None
    blank_lines = bool(scenario.get("blank_lines", False))

    with Path(os.environ["UCI_ENGINE_LOG"]).open("a", encoding="utf-8") as log:
        log.write("start\n")
        log.flush()
        current_fen: str | None = None
        for raw_line in sys.stdin:
            command = raw_line.strip()
            log.write(f"{command}\n")
            log.flush()
            if exit_on is not None and command.startswith(exit_on):
                return
            if command == "quit":
                return
            if command == "uci":
                print(f"id name {name}")
                print("id author adapter test double")
                if blank_lines:
                    print()
                print("uciok", flush=True)
            elif command == "isready":
                print("readyok", flush=True)
            elif command.startswith("position fen "):
                current_fen = command.removeprefix("position fen ")
            elif command.startswith("go "):
                if current_fen is not None:
                    for response in positions.get(current_fen, []):
                        print(response, flush=True)


if __name__ == "__main__":
    main()
