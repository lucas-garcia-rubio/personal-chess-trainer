from dataclasses import dataclass
import hashlib
import io
from typing import cast

import chess
import chess.pgn

from trainer.application.ports import LocalStorage, PositionEvaluator
from trainer.domain import GameMetadata, derive_import_analysis, derive_operational_instant


class ImportValidationError(ValueError):
    """The submitted document is not a supported Game for Import."""


class _ImportedGame(chess.pgn.Game):
    """A parsed Game that remembers the headers its document actually carried.

    python-chess fills the Seven Tag Roster with placeholders and lets the
    movetext marker overwrite ``Result``, so Import validates against the tags
    as submitted instead of the normalized header set.
    """

    document_headers: dict[str, str]
    movetext_result: str | None


class _ImportGameBuilder(chess.pgn.GameBuilder[_ImportedGame]):
    """Builds _ImportedGame so Import can validate the document as submitted."""

    def __init__(self) -> None:
        super().__init__(Game=_ImportedGame)

    def begin_game(self) -> None:
        super().begin_game()
        self.game.document_headers = {}
        self.game.movetext_result = None

    def visit_header(self, tagname: str, tagvalue: str) -> None:
        self.game.document_headers[tagname] = tagvalue
        super().visit_header(tagname, tagvalue)

    def visit_result(self, result: str) -> None:
        self.game.movetext_result = result
        super().visit_result(result)


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
        document = io.StringIO(raw_pgn)
        game = cast(
            _ImportedGame | None,
            chess.pgn.read_game(document, Visitor=_ImportGameBuilder),
        )
        if game is None:
            raise ImportValidationError("Paste one valid Standard Game in PGN format.")

        submitted = game.document_headers
        variant = submitted.get("Variant", "Standard")
        if variant.casefold() not in {
            "standard",
            "chess",
            "normal",
            "from position",
        }:
            raise ImportValidationError(
                f'Standard Games only; the variant "{variant}" is not supported.'
            )

        setup = submitted.get("SetUp")
        fen = submitted.get("FEN")
        if setup == "1" and fen is None:
            raise ImportValidationError('SetUp "1" requires a FEN header.')
        if fen is not None and setup != "1":
            raise ImportValidationError('A FEN header requires SetUp "1".')
        if setup not in (None, "0", "1"):
            raise ImportValidationError('The SetUp header must be "0" or "1".')

        if game.errors:
            # The parser suffixes its errors with "while parsing <Game ...>",
            # which carries memory addresses the Player cannot act on.
            detail = str(game.errors[0]).split(" while parsing ")[0]
            if fen is not None and "fen" in detail.casefold():
                raise ImportValidationError(f"Could not parse the FEN: {detail}")
            raise ImportValidationError(f"Could not parse the PGN: {detail}")
        if chess.pgn.read_game(document) is not None:
            raise ImportValidationError(
                "The PGN must contain exactly one Game; "
                "remove the second Game or any content left after the first one."
            )

        missing = [
            tag
            for tag in ("White", "Black", "Result")
            if submitted.get(tag) in (None, "", "?")
        ]
        if missing:
            raise ImportValidationError(
                f"The PGN must include the {', '.join(missing)} "
                f"header{'s' if len(missing) > 1 else ''}."
            )
        result_header = submitted["Result"]
        if result_header not in ("1-0", "0-1", "1/2-1/2"):
            raise ImportValidationError(
                'The Result header must be "1-0", "0-1" or "1/2-1/2".'
            )
        movetext_result = game.movetext_result or "*"
        if result_header != movetext_result:
            raise ImportValidationError(
                f'The Result header ("{result_header}") does not match the '
                f'movetext result ("{movetext_result}").'
            )

        board = game.board()
        if type(board) is not chess.Board or board.chess960:
            raise ImportValidationError("This Import accepts Standard Games only.")
        initial_fen = board.fen()

        headers = dict(game.headers)
        white = submitted["White"]
        black = submitted["Black"]
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
        played_at = derive_operational_instant(headers)
        time_control = headers.get("TimeControl", "-")
        analysis = derive_import_analysis(
            source_id=origin_id,
            played_at=played_at,
            white=white,
            black=black,
            result_header=result_header,
            speed=_speed(time_control),
            time_control=time_control,
            moves=moves,
            evaluation_run=evaluation_run,
            player_username=self._player_username,
            initial_fen=initial_fen,
        )
        metadata = GameMetadata(
            origin=origin,
            origin_id=origin_id,
            played_at=played_at,
            white=white,
            black=black,
            result=result_header,
            time_control=time_control,
            eco=headers.get("ECO"),
            opening=headers.get("Opening"),
            headers=headers,
        )
        self._storage.save_game(raw_pgn, metadata, analysis)
        return ImportedGame(origin=origin, origin_id=origin_id)


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
