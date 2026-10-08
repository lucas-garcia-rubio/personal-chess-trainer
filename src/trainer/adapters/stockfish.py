"""Stockfish adapter for the position-evaluation port.

Each evaluation operation starts one UCI process, reuses it for every requested
position and terminates it before returning — on success or failure. The domain
receives scores oriented from White's point of view, best moves and principal
variations in UCI notation, plus the engine's identity and effective parameters
as provenance. Critical Moment classification stays in the domain.
"""

from collections.abc import Sequence
from pathlib import Path
import queue
import re
import shutil
import subprocess
from threading import Thread
import time
from typing import IO, Self, cast

import chess

from trainer.config import EngineSettings
from trainer.domain import (
    EvaluationRun,
    EvaluationScore,
    EvaluatorProvenance,
    PositionEvaluation,
    ScoreKind,
)

_QUIT_TIMEOUT_SECONDS = 5.0
_EOF = ""
_VERSION = re.compile(r"\d+(?:\.\d+)*")


class StockfishError(RuntimeError):
    """Raised when Stockfish is unavailable or violates the UCI protocol."""


class StockfishEvaluator:
    """Evaluates positions through one Stockfish UCI process per operation."""

    def __init__(self, settings: EngineSettings) -> None:
        self._settings = settings

    def evaluate_positions(self, positions: Sequence[str]) -> EvaluationRun:
        deadline = time.monotonic() + self._settings.timeout_seconds
        with _UciSession(self._command(), self._settings, deadline) as session:
            provenance = session.provenance()
            evaluations = [session.evaluate(fen) for fen in positions]
        return EvaluationRun(provenance=provenance, evaluations=evaluations)

    def _command(self) -> list[str]:
        executable = self._settings.executable
        if executable is None:
            found = shutil.which("stockfish")
            if found is None:
                raise StockfishError(
                    "No Stockfish executable found on PATH. "
                    "Install Stockfish or set [engine] path."
                )
            return [found]
        return [str(executable)]


class _UciSession:
    """One UCI conversation, from the ``uci`` handshake to ``quit``."""

    def __init__(
        self, command: list[str], settings: EngineSettings, deadline: float
    ) -> None:
        self._command = command
        self._settings = settings
        self._deadline = deadline
        self._process: subprocess.Popen[str]
        self._lines: queue.Queue[str] = queue.Queue()
        self._reader: Thread
        self._engine_name: str | None = None

    def __enter__(self) -> Self:
        self._start()
        try:
            self._handshake()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()

    def _start(self) -> None:
        try:
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as error:
            raise StockfishError(
                f"Could not start the configured engine executable {self._command[0]}. "
                "Set [engine] path to an existing Stockfish binary."
            ) from error
        self._reader = Thread(
            target=_drain, args=(cast(IO[str], self._process.stdout), self._lines),
            daemon=True,
        )
        self._reader.start()

    def _handshake(self) -> None:
        self._send("uci")
        while True:
            line = self._read("uciok")
            if line.startswith("id name "):
                self._engine_name = line.removeprefix("id name ").strip()
            elif line == "uciok":
                break
        self._send(f"setoption name Threads value {self._settings.threads}")
        self._send(f"setoption name Hash value {self._settings.hash_mb}")
        self._send("isready")
        while self._read("readyok") != "readyok":
            pass

    def provenance(self) -> EvaluatorProvenance:
        name = self._engine_name if self._engine_name is not None else "unknown"
        return EvaluatorProvenance(
            source_kind="local-engine",
            name=name,
            version=_detect_version(name),
            parameters={
                "depth": self._settings.depth,
                "threads": self._settings.threads,
                "hash_mb": self._settings.hash_mb,
            },
        )

    def evaluate(self, fen: str) -> PositionEvaluation:
        self._send(f"position fen {fen}")
        self._send(f"go depth {self._settings.depth}")
        score: EvaluationScore | None = None
        variation: list[str] | None = None
        best_move: str | None = None
        while True:
            line = self._read(f"the evaluation of {fen}")
            if line.startswith("info"):
                parsed_score, parsed_variation = _parse_info(line)
                if parsed_score is not None:
                    score = parsed_score
                if parsed_variation is not None:
                    variation = parsed_variation
            elif line.startswith("bestmove"):
                tokens = line.split()
                if len(tokens) < 2:
                    raise StockfishError(
                        f"Stockfish returned a malformed bestmove line: {line!r}."
                    )
                best_move = None if tokens[1] in {"(none)", "0000"} else tokens[1]
                break
        if score is None:
            raise StockfishError(f"Stockfish reported no score for the position {fen}.")
        _validate_uci_moves(fen, best_move, variation)
        value = score.value
        if _black_to_move(fen):
            value = -value
        return PositionEvaluation(
            fen=fen,
            score=EvaluationScore(kind=score.kind, value=value),
            best_move=best_move,
            principal_variation=[] if variation is None else variation,
        )

    def _send(self, command: str) -> None:
        stdin = cast(IO[str], self._process.stdin)
        try:
            stdin.write(command + "\n")
            stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise StockfishError(
                f"Stockfish exited before receiving {command!r}."
            ) from error

    def _read(self, awaiting: str) -> str:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise self._timeout_error()
        try:
            line = self._lines.get(timeout=remaining)
        except queue.Empty as error:
            raise self._timeout_error() from error
        if line == _EOF:
            return_code = self._process.poll()
            raise StockfishError(
                f"Stockfish exited with code {return_code} while awaiting {awaiting}."
            )
        return line

    def _timeout_error(self) -> StockfishError:
        seconds = self._settings.timeout_seconds
        unit = "second" if seconds == 1 else "seconds"
        return StockfishError(
            "Stockfish exceeded the configured total timeout of "
            f"{seconds} {unit}. Increase [engine] timeout_seconds or reduce "
            "the Game length or engine depth."
        )

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if process is None:
            return
        try:
            if process.poll() is None:
                stdin = cast(IO[str], process.stdin)
                try:
                    stdin.write("quit\n")
                    stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    process.wait(timeout=_QUIT_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=_QUIT_TIMEOUT_SECONDS)
        finally:
            reader = getattr(self, "_reader", None)
            if reader is not None:
                reader.join(timeout=_QUIT_TIMEOUT_SECONDS)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


def _drain(stdout: IO[str], lines: queue.Queue[str]) -> None:
    for line in stdout:
        stripped = line.rstrip("\n")
        # Blank lines are protocol noise — Stockfish 19 emits one in its uci
        # reply — so the queue reserves "" exclusively for end-of-stream.
        if stripped:
            lines.put(stripped)
    lines.put(_EOF)


def _parse_info(line: str) -> tuple[EvaluationScore | None, list[str] | None]:
    """Read one ``info`` line, reporting its score and variation independently.

    Either may be absent, so a scored line without a ``pv`` never erases the
    variation of an earlier line.
    """
    tokens = line.split()
    if len(tokens) < 2 or tokens[1] == "string":
        return None, None
    score: EvaluationScore | None = None
    variation: list[str] | None = None
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token == "score" and index + 2 < len(tokens):
            kind = tokens[index + 1]
            if kind in ("cp", "mate"):
                try:
                    score = EvaluationScore(
                        kind=cast(ScoreKind, kind), value=int(tokens[index + 2])
                    )
                except ValueError:
                    pass
                index += 3
                continue
        if token == "pv":
            variation = tokens[index + 1 :]
            break
        index += 1
    return score, variation


def _detect_version(name: str) -> str | None:
    for token in reversed(name.split()):
        if _VERSION.fullmatch(token):
            return token
    return None


def _validate_uci_moves(
    fen: str, best_move: str | None, variation: list[str] | None
) -> None:
    if best_move is not None:
        try:
            chess.Board(fen).parse_uci(best_move)
        except ValueError as error:
            raise StockfishError(
                f"Stockfish returned the invalid UCI move {best_move!r}."
            ) from error

    board = chess.Board(fen)
    for move_text in variation or []:
        try:
            move = board.parse_uci(move_text)
        except ValueError as error:
            raise StockfishError(
                f"Stockfish returned the invalid UCI move {move_text!r}."
            ) from error
        board.push(move)


def _black_to_move(fen: str) -> bool:
    return fen.split()[1] == "b"
