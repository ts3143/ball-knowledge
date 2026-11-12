"""The golden vectors: core Glicko-2 cases and replay cases with the engine's outputs, for the TypeScript port.
They pin their own parameters (not tunables.toml), so retuning never makes the file stale; only a change in the
engine's behaviour does. `golden` writes tests/glicko2_cases.json, `golden --check` fails if it is out of date.
All ids and features are placeholders."""

import dataclasses
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

from . import ENGINE_VERSION
from .core import DegeneratePeriodError, Opponent, rate_period
from .engine import replay
from .params import Params, SeedParams
from .seed import prompt_features
from .types import Game, Prompt

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "tests" / "glicko2_cases.json"
RELATIVE_TOLERANCE = 1e-6  # what the TypeScript port must match
SEED = 20261112
CORE_RANDOM_CASES = 50
POINTS = [0, 10, 15, 25, 50, 75, 100]

# Glickman's worked example, at the precision the paper prints: it works with rounded intermediates (our 1464.0507
# is its 1464.06) and truncates the volatility 0.059996 to 0.05999.
GLICKMAN = {
    "inputs": {"rating": 1500.0, "rd": 200.0, "volatility": 0.06,
               "opponents": [(1400.0, 30.0, 1.0), (1550.0, 100.0, 0.0), (1700.0, 300.0, 0.0)]},
    "published": {"rating": 1464.06, "rd": 151.52, "volatility": 0.05999},
    "published_abs_tolerance": {"rating": 0.01, "rd": 0.01, "volatility": 0.00001},
}


def golden_params(**overrides) -> Params:
    """Fixed parameters of the golden file (standard Glicko-2 unless a case overrides)."""
    base = Params(initial_rating=1500.0, initial_rd=350.0, initial_volatility=0.06, tau=0.5,
                  volatility_epsilon=0.000001, child_scope_initial_rd=250.0, game_weight=1.0,
                  uncertain_rd_threshold=110.0, club_rating_min_prompts=20, prompt_lock_answers=200,
                  points_scale=100.0, seed=SeedParams(1500.0, 350.0, {"log_recall_mass": 40.0}))
    return dataclasses.replace(base, **overrides)


def core_case(name: str, rating: float, rd: float, volatility: float, idle_periods: int,
              opponents: list[tuple[float, float, float]], params: Params) -> dict:
    out = rate_period(rating, rd, volatility, idle_periods,
                      [Opponent(*o) for o in opponents], params)
    return {
        "name": name,
        "params": {"tau": params.tau, "volatility_epsilon": params.volatility_epsilon,
                   "game_weight": params.game_weight, "initial_rd": params.initial_rd},
        "input": {"rating": rating, "rd": rd, "volatility": volatility, "idle_periods": idle_periods,
                  "opponents": [{"rating": r, "rd": d, "score": s} for r, d, s in opponents]},
        "expected": {"rating": out.rating, "rd": out.rd, "volatility": out.volatility},
    }


def core_error_case(name: str, rating: float, rd: float, volatility: float,
                    opponents: list[tuple[float, float, float]], params: Params) -> dict:
    """A period the engine must refuse: every expected result saturates to exactly 0 or 1."""
    try:
        rate_period(rating, rd, volatility, 0, [Opponent(*o) for o in opponents], params)
    except DegeneratePeriodError:
        pass
    else:
        raise AssertionError(f"{name} did not raise")
    return {
        "name": name,
        "params": {"tau": params.tau, "volatility_epsilon": params.volatility_epsilon,
                   "game_weight": params.game_weight, "initial_rd": params.initial_rd},
        "input": {"rating": rating, "rd": rd, "volatility": volatility, "idle_periods": 0,
                  "opponents": [{"rating": r, "rd": d, "score": s} for r, d, s in opponents]},
        "expected_error": "degenerate",
    }


# prompts rows as the database stores them (placeholder numbers): rarity and gate_results jsonb
FEATURE_CASES = [
    ("typical", {"log_recall_mass": 11.25, "version": "x"}, {"tiers": {"n_eff": 7.5, "top_share": 0.31}}),
    ("n_eff_one", {"log_recall_mass": -3.5}, {"tiers": {"n_eff": 1}}),
    ("n_eff_large", {"log_recall_mass": 0}, {"tiers": {"n_eff": 110.25}}),
]
FEATURE_ERROR_CASES = [
    ("n_eff_zero", {"log_recall_mass": 1.0}, {"tiers": {"n_eff": 0}}),
    ("n_eff_negative", {"log_recall_mass": 1.0}, {"tiers": {"n_eff": -2.0}}),
    ("n_eff_missing", {"log_recall_mass": 1.0}, {"tiers": {}}),
    ("tiers_missing", {"log_recall_mass": 1.0}, {}),
    ("recall_missing", {}, {"tiers": {"n_eff": 4.0}}),
    ("recall_null", {"log_recall_mass": None}, {"tiers": {"n_eff": 4.0}}),
    ("n_eff_string", {"log_recall_mass": 1.0}, {"tiers": {"n_eff": "7.5"}}),
]


def feature_cases() -> list[dict]:
    out = [{"name": n, "rarity": r, "gate_results": gr, "expected": prompt_features(r, gr)}
           for n, r, gr in FEATURE_CASES]
    for n, r, gr in FEATURE_ERROR_CASES:
        try:
            prompt_features(r, gr)
        except ValueError:
            out.append({"name": n, "rarity": r, "gate_results": gr, "expected_error": "invalid"})
        else:
            raise AssertionError(f"{n} did not raise")
    return out


def core_cases() -> list[dict]:
    rng = random.Random(SEED)
    cases = []
    g = GLICKMAN["inputs"]
    case = core_case("glickman_example", g["rating"], g["rd"], g["volatility"], 0, g["opponents"],
                     golden_params())
    case["published"], case["published_abs_tolerance"] = GLICKMAN["published"], GLICKMAN["published_abs_tolerance"]
    cases.append(case)
    # No games: exact idle growth, and the cap.
    cases.append(core_case("idle_3_periods", 1500.0, 50.0, 0.06, 3, [], golden_params()))
    cases.append(core_case("idle_year_capped_growth", 1620.0, 50.0, 0.06, 364, [], golden_params()))
    cases.append(core_case("idle_hits_cap", 1500.0, 340.0, 0.06, 500, [], golden_params()))
    # Extremes stay finite.
    cases.append(core_case("rd_1_big_gap", 1500.0, 1.0, 0.06, 0, [(4500.0, 30.0, 0.0), (1500.0, 1.0, 0.5)],
                           golden_params()))
    cases.append(core_case("rd_350_big_gap", 1500.0, 350.0, 0.06, 0, [(-1500.0, 350.0, 1.0)], golden_params()))
    cases.append(core_case("fractional_results_w2", 1500.0, 350.0, 0.06, 0,
                           [(1500.0, 350.0, 0.25), (1600.0, 350.0, 0.15)], golden_params(game_weight=2.0)))
    # Saturated expected results: E is exactly 1 (and, far enough out, exp overflows): the period is refused.
    cases.append(core_error_case("saturated_expected_one", 1500.0, 50.0, 0.06, [(-6000.0, 30.0, 1.0)], golden_params()))
    cases.append(core_error_case("saturated_exp_overflow", 1500.0, 50.0, 0.06, [(200000.0, 30.0, 0.0)],
                                 golden_params()))
    for i in range(CORE_RANDOM_CASES):
        n = rng.randint(1, 10)
        opponents = [(round(rng.uniform(1000, 2200), 3), round(rng.uniform(30, 350), 3), rng.choice(POINTS) / 100.0)
                     for _ in range(n)]
        params = golden_params(tau=rng.choice([0.2, 0.5, 0.8, 1.2]), game_weight=rng.choice([1.0, 1.5, 3.0]))
        cases.append(core_case(f"random_{i:02d}", round(rng.uniform(1100, 1900), 3), round(rng.uniform(30, 350), 3),
                               round(rng.uniform(0.02, 0.1), 4), rng.choice([0, 0, 1, 5, 30]), opponents, params))
    return cases


def _dive(day: date, user: int, attempt: int, prompt_ids: list[int], rng: random.Random, answered: int | None = None,
          rated: bool = True) -> list[Game]:
    """One attempt: a Game per prompt, random points; prompts past `answered` are unanswered."""
    games = []
    for i, pid in enumerate(prompt_ids):
        unanswered = answered is not None and i >= answered
        games.append(Game(user, day, attempt, pid, 0 if unanswered else rng.choice(POINTS), rated, unanswered))
    return games


def _prompts(ids: list[int], competition: int, clubs: dict[int, tuple[int, ...]] | None = None) -> list[Prompt]:
    return [Prompt(i, competition, (clubs or {}).get(i, ()), {"log_recall_mass": -1.0 - (i % 7) * 0.5}) for i in ids]


def _two_feature_prompts(ids: list[int], competition: int) -> list[Prompt]:
    """Prompts with both seed features and a template offset that changes with the id."""
    return [Prompt(i, competition, (), {"log_recall_mass": 9.5 + (i % 7) * 0.75, "ln_n_eff": 1.0 + (i % 5) * 0.8},
                   (12.5, -30.0, 0.0)[i % 3]) for i in ids]


def replay_scenarios() -> list[tuple[str, Params, list[Prompt], list[Game]]]:
    rng = random.Random(SEED + 1)
    d0 = date(2030, 1, 1)
    day = lambda n: d0 + timedelta(days=n)
    out = []

    # Three users over three consecutive dates; clubs 11 and 12 tagged on some prompts.
    ids = list(range(1, 22))
    clubs = {i: ((11,) if i % 2 else (12,)) for i in ids if i % 3 != 0}
    games = []
    for d in range(3):
        block = ids[d * 7:(d + 1) * 7]
        for user in (1, 2, 3):
            if not (user == 3 and d == 1):
                games += _dive(day(d), user, 1, block, rng)
    out.append(("multi_scope", golden_params(), _prompts(ids, 1, clubs), games))

    # A club scope that first appears on date 3: it starts from the competition's state at the start of that date.
    ids = list(range(1, 22))
    clubs = {i: (21,) for i in range(15, 22)}
    games = []
    for d in range(3):
        for user in (1, 2):
            games += _dive(day(d), user, 1, ids[d * 7:(d + 1) * 7], rng)
    out.append(("child_scope_start", golden_params(child_scope_initial_rd=200.0, game_weight=1.5),
                _prompts(ids, 1, clubs), games))

    # An idle gap: user 1 plays dates 0 and 20, user 2 plays 0, 1 and 2 (to stay active).
    ids = list(range(1, 22))
    games = _dive(day(0), 1, 1, ids[:7], rng) + _dive(day(20), 1, 1, ids[7:14], rng)
    for d in range(3):
        games += _dive(day(d), 2, 1, ids[14:21] if d == 0 else ids[:7] if d == 1 else ids[7:14], rng)
    out.append(("idle_gap", golden_params(tau=0.3), _prompts(ids, 1), games))

    # Two competitions on one date: one user plays both attempts; club 31 is tagged in both (lowest id parents it).
    p = (_prompts(list(range(1, 8)), 2, {1: (31,), 2: (31,)}) + _prompts(list(range(8, 15)), 1, {8: (31,), 9: (31,)})
         + _prompts(list(range(15, 22)), 1, {15: (31,)}))
    games = _dive(day(0), 1, 1, list(range(8, 15)), rng) + _dive(day(0), 1, 2, list(range(1, 8)), rng)
    games += _dive(day(0), 2, 1, list(range(1, 8)), rng)
    games += _dive(day(1), 1, 1, list(range(15, 22)), rng)
    out.append(("two_competitions_one_date", golden_params(), p, games))

    # A low lock threshold: prompt 1 locks after date 0 (3 answers) and is then left alone when replayed on date 1.
    ids = list(range(1, 8))
    games = []
    for user in (1, 2, 3):
        games += _dive(day(0), user, 1, ids, rng)
    for user in (1, 4):
        games += _dive(day(1), user, 1, ids, rng)
    out.append(("low_threshold_lock", golden_params(prompt_lock_answers=3), _prompts(ids, 1), games))

    # An abandoned attempt (3 of 7 answered, still 7 games) and an archive play that must be ignored.
    ids = list(range(1, 15))
    games = _dive(day(0), 1, 1, ids[:7], rng, answered=3)
    games += _dive(day(0), 2, 1, ids[:7], rng)
    games += _dive(day(1), 1, 1, ids[7:14], rng, rated=False)
    games += _dive(day(1), 2, 1, ids[7:14], rng)
    out.append(("abandoned_attempt", golden_params(), _prompts(ids, 1), games))

    # Two users in two competitions share club 41 on one date: each user's club parent is that user's own first
    # prompt touching it (user 1 -> competition 1, user 2 -> competition 2), whatever the other user plays.
    p = (_prompts(list(range(1, 8)), 1, {1: (41,)}) + _prompts(list(range(8, 15)), 2, {8: (41,)})
         + _prompts(list(range(15, 22)), 1, {15: (41,)}) + _prompts(list(range(22, 29)), 2))
    games = _dive(day(0), 1, 1, list(range(22, 29)), rng) + _dive(day(0), 2, 1, list(range(22, 29)), rng)
    games += _dive(day(1), 1, 1, list(range(1, 8)), rng) + _dive(day(1), 2, 1, list(range(8, 15)), rng)
    games += _dive(day(2), 1, 1, list(range(15, 22)), rng)
    out.append(("two_users_shared_club", golden_params(), p, games))

    # A two-coefficient seed map with template offsets (the shape of the fitted tunables).
    ids = list(range(1, 15))
    seed = SeedParams(2476.79, 60.0, {"log_recall_mass": -71.0242, "ln_n_eff": 36.3276})
    games = []
    for d in range(2):
        for user in (1, 2):
            games += _dive(day(d), user, 1, ids[d * 7:(d + 1) * 7], rng)
    out.append(("two_feature_seed_offsets", golden_params(seed=seed), _two_feature_prompts(ids, 1), games))

    # Calendar edges: a year end, a leap day (2032-02-29, skipped between the 28th and 1 March), a month end and
    # a non-leap February, one 7-prompt dive per date.
    dates = [date(2031, 12, 30), date(2032, 1, 2), date(2032, 1, 31), date(2032, 2, 28), date(2032, 3, 1),
             date(2033, 2, 28), date(2033, 3, 1)]
    ids = list(range(1, 50))
    games = []
    for k, d in enumerate(dates):
        games += _dive(d, 1, 1, ids[k * 7:(k + 1) * 7], rng)
    for k in (0, 1, 3):
        games += _dive(dates[k], 2, 1, ids[k * 7:(k + 1) * 7], rng)
    out.append(("calendar_edges", golden_params(), _prompts(ids, 1, {i: (51,) for i in ids if i % 4 == 0}), games))
    return out


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def replay_case(name: str, params: Params, prompts: list[Prompt], games: list[Game]) -> dict:
    result = replay(prompts, games, params)
    return {
        "name": name,
        "params": params.to_dict(),
        "prompts": [{"id": p.id, "competition_id": p.competition_id, "club_ids": list(p.club_ids),
                     "features": dict(p.features), "template_offset": p.template_offset, "sport": p.sport}
                    for p in prompts],
        "games": [{"user_id": g.user_id, "date": _iso(g.date), "attempt": g.attempt, "prompt_id": g.prompt_id,
                   "points": g.points, "rated": g.rated, "unanswered": g.unanswered} for g in games],
        "expected": {
            "scopes": [{"user_id": u, "scope_type": s[0], "scope_id": s[1], "rating": st.rating, "rd": st.rd,
                        "volatility": st.volatility, "games": st.games, "last_period": _iso(st.last_period)}
                       for (u, s), st in result.scopes.items()],
            "history": [{**dataclasses.asdict(h), "date": _iso(h.date)} for h in result.history],
            "prompts": [{"prompt_id": pid, **dataclasses.asdict(ps), "last_period": _iso(ps.last_period)}
                        for pid, ps in result.prompts.items()],
        },
    }


def build() -> dict:
    return {
        "engine_version": ENGINE_VERSION,
        "relative_tolerance": RELATIVE_TOLERANCE,
        "core": core_cases(),
        "features": feature_cases(),
        "replay": [replay_case(*s) for s in replay_scenarios()],
    }


def render(doc: dict) -> str:
    return json.dumps(doc, indent=1) + "\n"


def differences(a, b, path: str = "") -> list[str]:
    """Where two JSON documents differ; floats compare to 1e-9 relative so a different libm cannot fail it."""
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return [f"{path}: keys differ"]
        return [d for k in a for d in differences(a[k], b[k], f"{path}/{k}")]
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in differences(x, y, f"{path}[{i}]")]
    if isinstance(a, float) and isinstance(b, float):
        return [] if math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12) else [f"{path}: {a} != {b}"]
    return [] if a == b and type(a) is type(b) else [f"{path}: {a!r} != {b!r}"]


def stale_reasons(path: Path = GOLDEN_PATH) -> list[str]:
    if not path.exists():
        return [f"{path} does not exist"]
    return differences(json.loads(path.read_text()), json.loads(render(build())))


def write(path: Path = GOLDEN_PATH) -> None:
    path.write_text(render(build()))
