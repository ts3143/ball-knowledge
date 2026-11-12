"""Input checks shared by the engine entry points. The TypeScript port (lib/glicko2) rejects the same inputs:
dates must be real calendar dates, numbers must be finite, results must lie in [0, 1], a user plays a prompt once
per date, and a prompt lists each club once."""

import math
from collections.abc import Iterable
from datetime import date
from numbers import Real

from .params import Params
from .types import Game, Prompt


def finite(x: object, name: str) -> float:
    """x as a float; bool, non-numbers, nan and infinities raise ValueError."""
    if isinstance(x, bool) or not isinstance(x, Real) or not math.isfinite(x):
        raise ValueError(f"{name} must be a finite number, got {x!r}")
    return float(x)


def check_date(d: object, name: str = "date") -> date:
    """A plain date: a datetime (or a string) raises ValueError."""
    if type(d) is not date:
        raise ValueError(f"{name} must be a datetime.date, got {d!r}")
    return d


def check_params(params: Params) -> None:
    for name in ("initial_rating", "initial_rd", "initial_volatility", "tau", "volatility_epsilon",
                 "child_scope_initial_rd", "game_weight", "uncertain_rd_threshold", "club_rating_min_prompts",
                 "prompt_lock_answers", "points_scale"):
        finite(getattr(params, name), f"params.{name}")
    for name in ("initial_rd", "initial_volatility", "tau", "volatility_epsilon", "child_scope_initial_rd",
                 "game_weight", "points_scale"):
        if getattr(params, name) <= 0:
            raise ValueError(f"params.{name} must be above 0, got {getattr(params, name)!r}")
    finite(params.seed.intercept, "params.seed.intercept")
    finite(params.seed.rd, "params.seed.rd")
    for k, v in params.seed.coefficients.items():
        finite(v, f"params.seed.coefficients.{k}")


def check_prompts(prompts: Iterable[Prompt]) -> None:
    for p in prompts:
        if len(set(p.club_ids)) != len(p.club_ids):
            raise ValueError(f"prompt {p.id} lists a club twice: {list(p.club_ids)}")
        finite(p.template_offset, f"prompt {p.id} template_offset")
        for k, v in p.features.items():
            finite(v, f"prompt {p.id} feature {k}")


def check_games(games: Iterable[Game], points_scale: float) -> None:
    """Rated games only. A game that is unanswered scores 0 whatever its points say, so its points are not checked."""
    seen: set[tuple[int, date, int]] = set()
    for g in games:
        check_date(g.date, f"game date (user {g.user_id}, prompt {g.prompt_id})")
        key = (g.user_id, g.date, g.prompt_id)
        if key in seen:
            raise ValueError(f"duplicate rated game: user {g.user_id} on {g.date.isoformat()} prompt {g.prompt_id} "
                             "(one rated attempt per user per dive)")
        seen.add(key)
        if not g.unanswered:
            points = finite(g.points, f"points (user {g.user_id}, prompt {g.prompt_id})")
            if not 0.0 <= points <= points_scale:
                raise ValueError(f"points {points:g} outside 0..{points_scale:g} "
                                 f"(user {g.user_id}, prompt {g.prompt_id})")
