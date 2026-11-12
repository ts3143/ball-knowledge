"""Inputs and outputs of the engine. Ids are ints; dates are datetime.date."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

SPORT, COMPETITION, CLUB = "sport", "competition", "club"
SCOPE_ORDER = {SPORT: 0, COMPETITION: 1, CLUB: 2}

# (scope_type, scope_id): the sport's id is its name ("football"), a competition's or club's is its entity id.
Scope = tuple[str, int | str]


def scope_key(scope: Scope) -> tuple[int, int | str]:
    """Sort key: sport, then competitions, then clubs, each by id."""
    return SCOPE_ORDER[scope[0]], scope[1]


@dataclass(frozen=True)
class State:
    """One player's rating in one scope, on the Glicko-1 scale (1500 centre)."""
    rating: float
    rd: float
    volatility: float
    games: int
    last_period: date | None


@dataclass(frozen=True)
class Prompt:
    id: int
    competition_id: int
    club_ids: tuple[int, ...] = ()
    features: Mapping[str, float] = field(default_factory=dict)  # seed features, frozen at approval
    template_offset: float = 0.0
    sport: str = "football"


@dataclass(frozen=True)
class Game:
    """One prompt result of one rated attempt. The caller resolves these from `answers`: archive plays carry
    rated=False, and a prompt that was never served or answered in a started attempt is a row with
    unanswered=True, which scores 0 whatever `points` says."""
    user_id: int
    date: date  # the dive date
    attempt: int  # orders a user's attempts within a date (attempt start order)
    prompt_id: int
    points: int
    rated: bool = True
    unanswered: bool = False


@dataclass(frozen=True)
class PromptState:
    rating: float
    rd: float
    volatility: float
    games: int
    locked: bool
    last_period: date | None
    seed_rating: float
    seed_rd: float


@dataclass(frozen=True)
class HistoryRow:
    """One row per (attempt, scope the attempt touched). `before` is the state the period started from, or
    the state after the previous attempt of the same date (idle growth and child-scope init applied);
    `after` includes every game of the date so far. `games` and `score_sum` cover the date's period so far."""
    user_id: int
    date: date
    attempt: int
    scope_type: str
    scope_id: int | str
    rating_before: float
    rd_before: float
    volatility_before: float
    rating_after: float
    rd_after: float
    volatility_after: float
    games: int
    score_sum: float


@dataclass(frozen=True)
class ReplayResult:
    scopes: dict[tuple[int, Scope], State]  # keyed (user_id, scope)
    history: list[HistoryRow]
    prompts: dict[int, PromptState]
