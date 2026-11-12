"""What a player sees: RD today (computed, never stored), the uncertain marker, club visibility."""

from datetime import date

from .core import idle_rd
from .params import Params
from .types import State


def idle_periods(last_period: date, today: date) -> int:
    """Dive dates skipped between last_period and `today`: none when today is the next day."""
    return max(0, (today - last_period).days - 1)


def rd_now(state: State | None, today: date, params: Params) -> float:
    """RD with idle growth up to `today`, as the player would start a dive on that date."""
    if state is None or state.last_period is None:
        return params.initial_rd
    return idle_rd(state.rd, state.volatility, idle_periods(state.last_period, today), params.initial_rd)


def is_uncertain(state: State | None, today: date, params: Params) -> bool:
    return rd_now(state, today, params) > params.uncertain_rd_threshold


def club_visible(state: State | None, params: Params) -> bool:
    return state is not None and state.games >= params.club_rating_min_prompts
