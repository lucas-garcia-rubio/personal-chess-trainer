from dataclasses import asdict, dataclass
import math
from typing import NotRequired, TypedDict

import chess


class UserDocument(TypedDict):
    name: str
    id: str


class PlayerDocument(TypedDict):
    user: UserDocument


class PlayersDocument(TypedDict):
    white: PlayerDocument
    black: PlayerDocument


class EvaluationDocument(TypedDict):
    eval: NotRequired[int]
    mate: NotRequired[int]
    best: NotRequired[str]


class ClockDocument(TypedDict):
    initial: int
    increment: int


class GameDocument(TypedDict):
    id: str
    createdAt: int
    speed: str
    status: str
    players: PlayersDocument
    moves: str
    analysis: list[EvaluationDocument]
    clock: ClockDocument
    winner: NotRequired[str]


@dataclass(frozen=True)
class CriticalMoment:
    ply: int
    position_fen: str
    played: str
    best: str
    classification: str
    win_before: float
    win_after: float


@dataclass(frozen=True)
class Analysis:
    source_id: str
    created_at: int
    opponent: str
    result: str
    speed: str
    time_control: str
    player_color: str
    critical_moments: list[CriticalMoment]

    def to_document(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class GameSummary:
    source_id: str
    opponent: str
    result: str
    speed: str
    critical_count: int


def _winning_chances(centipawns: int) -> float:
    capped = max(-1000, min(1000, centipawns))
    return 2 / (1 + math.exp(-0.00368208 * capped)) - 1


def _score(entry: EvaluationDocument) -> tuple[str, int] | None:
    if "eval" in entry:
        return ("cp", entry["eval"])
    if "mate" in entry:
        return ("mate", entry["mate"])
    return None


def _pov_value(score: tuple[str, int], white: bool) -> int:
    value = score[1]
    return value if white else -value


def _win_percent(score: tuple[str, int], white: bool) -> float:
    kind, value = score
    pov_value = value if white else -value
    centipawns = pov_value if kind == "cp" else (1000 if pov_value > 0 else -1000)
    return 50 + 50 * _winning_chances(centipawns)


def _classify(
    previous: tuple[str, int], current: tuple[str, int], mover_is_white: bool
) -> str | None:
    if previous[0] == current[0] == "cp":
        before = _winning_chances(_pov_value(previous, mover_is_white))
        after = _winning_chances(_pov_value(current, mover_is_white))
        drop = before - after
        if drop >= 0.3:
            return "Blunder"
        if drop >= 0.2:
            return "Mistake"
        if drop >= 0.1:
            return "Inaccuracy"
        return None

    previous_value = _pov_value(previous, mover_is_white)
    current_value = _pov_value(current, mover_is_white)
    created_mate = previous[0] == "cp" and current[0] == "mate" and current_value < 0
    lost_mate = (
        previous[0] == "mate"
        and previous_value > 0
        and (current[0] == "cp" or current_value < 0)
    )
    if created_mate:
        if previous_value < -999:
            return "Inaccuracy"
        if previous_value < -700:
            return "Mistake"
        return "Blunder"
    if lost_mate:
        if current[0] == "cp" and current_value > 999:
            return "Inaccuracy"
        if current[0] == "cp" and current_value > 700:
            return "Mistake"
        return "Blunder"
    return None


def derive_analysis(game: GameDocument, player_username: str) -> Analysis:
    white_name = game["players"]["white"]["user"]["name"]
    black_name = game["players"]["black"]["user"]["name"]
    player_is_white = white_name.casefold() == player_username.casefold()
    opponent = black_name if player_is_white else white_name
    winner = game.get("winner")
    player_color = "white" if player_is_white else "black"
    result = "draw" if winner is None else ("win" if winner == player_color else "loss")

    board = chess.Board()
    previous: tuple[str, int] | None = ("cp", 15)
    moments: list[CriticalMoment] = []
    moves = game["moves"].split()
    for index, san in enumerate(moves):
        if index >= len(game["analysis"]):
            break
        evaluation = game["analysis"][index]
        current = _score(evaluation)
        mover_is_white = board.turn == chess.WHITE
        move = board.parse_san(san)
        if (
            mover_is_white == player_is_white
            and previous is not None
            and current is not None
            and "best" in evaluation
        ):
            classification = _classify(previous, current, mover_is_white)
            if classification is not None:
                best_move = board.parse_uci(evaluation["best"])
                moments.append(
                    CriticalMoment(
                        ply=index + 1,
                        position_fen=board.fen(),
                        played=san,
                        best=board.san(best_move),
                        classification=classification,
                        win_before=round(_win_percent(previous, player_is_white), 1),
                        win_after=round(_win_percent(current, player_is_white), 1),
                    )
                )
        board.push(move)
        previous = current

    clock = game["clock"]
    return Analysis(
        source_id=game["id"],
        created_at=game["createdAt"],
        opponent=opponent,
        result=result,
        speed=game["speed"],
        time_control=f'{clock["initial"]}+{clock["increment"]}',
        player_color=player_color,
        critical_moments=moments,
    )
