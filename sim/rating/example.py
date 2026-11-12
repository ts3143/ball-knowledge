"""`python -m rating example`: the engine in plain language. Placeholder ids and names only."""

import dataclasses
from datetime import date, timedelta

from .core import Opponent, rate_period
from .display import club_visible, is_uncertain, rd_now
from .engine import replay
from .golden import GLICKMAN, golden_params
from .params import Params
from .types import CLUB, COMPETITION, SPORT, Game, Prompt, State

NAMES = {(SPORT, "football"): "Football (sport)", (COMPETITION, 1): "Competition A", (CLUB, 101): "Club A"}
SCOPE_ORDER = [(SPORT, "football"), (COMPETITION, 1), (CLUB, 101)]
# Points per prompt for each of the five dives (a tier's points, 15 for a Hipster Pick, 0 for a miss).
DIVES = [[10, 25, 0, 10, 50, 10, 25], [25, 50, 10, 75, 25, 10, 0], [50, 75, 25, 100, 50, 25, 10],
         [75, 50, 100, 25, 50, 75, 10], [100, 75, 50, 100, 25, 75, 50]]
DEMO_SEED_SLOPE = 30.0  # demo only: spreads the prompts' starting ratings so the opponents differ


def glickman(params: Params) -> None:
    g = GLICKMAN
    i = g["inputs"]
    out = rate_period(i["rating"], i["rd"], i["volatility"], 0, [Opponent(*o) for o in i["opponents"]],
                      golden_params())
    print("(a) Glickman's worked example")
    print("    A player rated 1500 (RD 200, volatility 0.06) plays three opponents: a win against 1400 (RD 30),")
    print("    a loss against 1550 (RD 100) and a loss against 1700 (RD 300). tau = 0.5, standard weight 1.")
    print(f"    {'':12}{'ours':>14}{'published':>12}")
    for label, ours, key, digits in (("new rating", out.rating, "rating", 2), ("new RD", out.rd, "rd", 2),
                                     ("volatility", out.volatility, "volatility", 5)):
        print(f"    {label:12}{ours:>14.6f}{g['published'][key]:>12.{digits}f}")
    print(f"    (volatility step converged in {out.iterations} iterations)")


def worked_replay(params: Params) -> None:
    d0 = date(2026, 1, 1)
    days = [d0, d0 + timedelta(days=1), d0 + timedelta(days=8), d0 + timedelta(days=9), d0 + timedelta(days=10)]
    # Dives 1-2: no club tags. Dive 3: six of seven prompts tag Club A, dives 4-5: all seven do.
    club_prompts = {2: 6, 3: 7, 4: 7}
    prompts, games = [], []
    for d, points in enumerate(DIVES):
        for k in range(7):
            pid = 100 * (d + 1) + k
            tagged = k < club_prompts.get(d, 0)
            prompts.append(Prompt(pid, 1, (101,) if tagged else (), {"log_recall_mass": -2.0 + 0.5 * (k % 5)}))
            games.append(Game(1, days[d], 1, pid, points[k]))
    slopes = {k: v or DEMO_SEED_SLOPE for k, v in params.seed.coefficients.items()}
    demo = dataclasses.replace(params, seed=dataclasses.replace(params.seed, coefficients=slopes))
    result = replay(prompts, games, demo)

    print()
    print("(b) A worked replay: one synthetic player, five dives in Competition A")
    print("    Rating and RD are shown before and after each dive. Every dive is one rating period for each scope")
    print(f"    it touches. Uncertain marker: RD above {demo.uncertain_rd_threshold:g}. A club rating is shown from "
          f"{demo.club_rating_min_prompts} prompts.")
    print(f"    Prompt ratings here start at {demo.seed.intercept:g} + {DEMO_SEED_SLOPE:g} x log_recall_mass "
          f"(demo values, not tuned).")
    by_key = {(h.date, h.scope_type, h.scope_id): h for h in result.history}
    prompts_seen: dict = {}  # prompts per scope so far (club visibility)
    for n, day in enumerate(days, 1):
        gap = (day - days[n - 2]).days - 1 if n > 1 else 0
        note = f"   <- {gap} dive dates skipped, so RD grows before the dive" if gap > 0 else ""
        print(f"\n  Dive {n}, {day.isoformat()}: {sum(DIVES[n - 1])} points of 700{note}")
        for scope in SCOPE_ORDER:
            h = by_key.get((day, *scope))
            if h is None:
                if scope == (CLUB, 101):
                    print(f"    {NAMES[scope]:18} not touched yet (no club prompt)")
                continue
            first = f" (new: starts at its parent's rating, RD at least {demo.child_scope_initial_rd:g})" \
                if scope == (CLUB, 101) and day == days[2] else ""
            st = State(h.rating_after, h.rd_after, h.volatility_after, h.games, day)
            total = prompts_seen[scope] = prompts_seen.get(scope, 0) + h.games
            flag = "uncertain" if is_uncertain(st, day, demo) else "settled"
            extra = ""
            if scope[0] == CLUB:
                extra = ", rating shown" if total >= demo.club_rating_min_prompts else \
                    f", rating hidden ({total} of {demo.club_rating_min_prompts} prompts)"
            print(f"    {NAMES[scope]:18} rating {h.rating_before:7.1f} -> {h.rating_after:7.1f}   "
                  f"RD {h.rd_before:6.1f} -> {h.rd_after:6.1f}   {flag}{extra}{first}")
    last = days[-1]
    print(f"\n  A week after dive 5 ({(last + timedelta(days=7)).isoformat()}), RD if the player returned:")
    for scope in SCOPE_ORDER:
        st = result.scopes[(1, scope)]
        later = last + timedelta(days=7)
        vis = f", rating {'shown' if club_visible(st, demo) else 'hidden'}" if scope[0] == CLUB else ""
        print(f"    {NAMES[scope]:18} RD {st.rd:6.1f} -> {rd_now(st, later, demo):6.1f}   "
              f"{'uncertain' if is_uncertain(st, later, demo) else 'settled'}{vis}")


def example(params: Params) -> None:
    glickman(params)
    worked_replay(params)
