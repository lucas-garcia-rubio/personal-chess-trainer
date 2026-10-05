from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io

import chess
import chess.pgn

from trainer.application.ports import LocalStorage, PositionEvaluator
from trainer.domain import GameMetadata, derive_import_analysis


class ImportValidationError(ValueError):
    """The submitted document is not a supported Game for Import."""


@dataclass(frozen=True)
class ImportedGame:
    origin: str
    origin_id: str


class ImportGame:
    def __init__(
        self,
        evaluator: PositionEvaluator,
        storage: LocalStorage,
        player_username: str,
    ) -> None:
        self._evaluator = evaluator
        self._storage = storage
        self._player_username = player_username

    def __call__(self, raw_pgn: str) -> ImportedGame:
        game = chess.pgn.read_game(io.StringIO(raw_pgn))
        if game is None:
            raise ImportValidationError("Paste one valid Standard Game in PGN format.")
        if game.errors:
            raise ImportValidationError(f"Could not parse the PGN: {game.errors[0]}")

        board = game.board()
        if type(board) is not chess.Board or board.chess960 or board.fen() != chess.STARTING_FEN:
            raise ImportValidationError(
                "This Import accepts Standard Games from the initial position."
            )

        headers = dict(game.headers)
        white = headers.get("White", "?")
        black = headers.get("Black", "?")
        username = self._player_username.casefold()
        if white.casefold() != username and black.casefold() != username:
            raise ImportValidationError(
                "The configured Player must be White or Black in the PGN."
            )

        moves = list(game.mainline_moves())
        positions = [board.fen()]
        for move in moves:
            board.push(move)
            positions.append(board.fen())
        evaluation_run = self._evaluator.evaluate_positions(positions)
        if len(evaluation_run.evaluations) != len(positions):
            raise RuntimeError("The evaluator did not return one evaluation per position.")

        origin = "content-sha256"
        origin_id = hashlib.sha256(raw_pgn.encode("utf-8")).hexdigest()
        played_at = _played_at(headers)
        time_control = headers.get("TimeControl", "-")
        analysis = derive_import_analysis(
            source_id=origin_id,
            played_at=played_at,
            white=white,
            black=black,
            result_header=headers.get("Result", "*"),
            speed=_speed(time_control),
            time_control=time_control,
            moves=moves,
            evaluation_run=evaluation_run,
            player_username=self._player_username,
        )
        metadata = GameMetadata(
            origin=origin,
            origin_id=origin_id,
            played_at=played_at,
            white=white,
            black=black,
            result=headers.get("Result", "*"),
            time_control=time_control,
            eco=headers.get("ECO"),
            opening=headers.get("Opening"),
            headers=headers,
        )
        self._storage.save_game(raw_pgn, metadata, analysis)
        return ImportedGame(origin=origin, origin_id=origin_id)


def _played_at(headers: dict[str, str]) -> int:
    for value, pattern in (
        (
            f'{headers.get("UTCDate", "")} {headers.get("UTCTime", "")}',
            "%Y.%m.%d %H:%M:%S",
        ),
        (headers.get("Date", ""), "%Y.%m.%d"),
    ):
        try:
            parsed = datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return int(parsed.timestamp() * 1000)
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _speed(time_control: str) -> str:
    try:
        initial, increment = (int(part) for part in time_control.split("+", 1))
    except (ValueError, TypeError):
        return "unknown"
    estimated = initial + 40 * increment
    if estimated < 180:
        return "bullet"
    if estimated < 480:
        return "blitz"
    if estimated < 1500:
        return "rapid"
    return "classical"
