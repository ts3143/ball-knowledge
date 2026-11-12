"""Replay of rated games into ratings: one rating period per (user, scope, dive date), and one per prompt.

Dates run in ascending order. Within a date every user is independent, and prompts are settled after all users
from the player states at the start of the date, so a prompt's rating is frozen for the whole date. After each
attempt a user's period for the date is recomputed from the state before the date using all the date's games so
far (in prompt order), so two attempts equal one batch. Nothing reads a clock.
"""

from collections import defaultdict
from datetime import date

from .core import Opponent, idle_rd, rate_period
from .display import idle_periods
from .params import Params
from .seed import seed_rating, seed_rd
from .types import (
    CLUB,
    COMPETITION,
    SPORT,
    Game,
    HistoryRow,
    Prompt,
    PromptState,
    ReplayResult,
    Scope,
    State,
    scope_key,
)


def club_parents(prompts: dict[int, Prompt], first_date: dict[int, date]) -> dict[int, int]:
    """A club's parent competition is that of the prompt that first touched it; the lowest id on a date tie."""
    best: dict[int, tuple[date, int]] = {}
    for pid in sorted(first_date):
        p = prompts[pid]
        for club in p.club_ids:
            candidate = (first_date[pid], p.competition_id)
            if club not in best or candidate < best[club]:
                best[club] = candidate
    return {club: c for club, (_, c) in best.items()}


def prompt_scopes(p: Prompt) -> list[Scope]:
    return [(SPORT, p.sport), (COMPETITION, p.competition_id), *[(CLUB, c) for c in sorted(p.club_ids)]]


def replay(prompts: list[Prompt], games: list[Game], params: Params) -> ReplayResult:
    prompt_by_id: dict[int, Prompt] = {}
    for p in prompts:
        if p.id in prompt_by_id:
            raise ValueError(f"duplicate prompt id {p.id}")
        prompt_by_id[p.id] = p
    rated = [g for g in games if g.rated]  # archive plays never enter the engine
    for g in rated:
        if g.prompt_id not in prompt_by_id:
            raise ValueError(f"game for unknown prompt {g.prompt_id}")

    by_date: dict[date, list[Game]] = defaultdict(list)
    first_date: dict[int, date] = {}
    for g in rated:
        by_date[g.date].append(g)
        if g.prompt_id not in first_date or g.date < first_date[g.prompt_id]:
            first_date[g.prompt_id] = g.date
    parent_club = club_parents(prompt_by_id, first_date)
    sport_of: dict[int, str] = {}  # competition -> sport
    for pid in sorted(prompt_by_id):
        p = prompt_by_id[pid]
        if sport_of.setdefault(p.competition_id, p.sport) != p.sport:
            raise ValueError(f"competition {p.competition_id} appears in two sports")

    def parent_of(scope: Scope) -> Scope | None:
        if scope[0] == CLUB:
            return COMPETITION, parent_club[scope[1]]
        if scope[0] == COMPETITION:
            return SPORT, sport_of[scope[1]]
        return None

    user_scopes: dict[int, dict[Scope, State]] = defaultdict(dict)
    prompt_states: dict[int, PromptState] = {}
    history: list[HistoryRow] = []

    for day in sorted(by_date):
        day_games = by_date[day]
        for pid in sorted({g.prompt_id for g in day_games}):
            if pid not in prompt_states:
                p = prompt_by_id[pid]
                seed = seed_rating(p.features, params.seed, p.template_offset)
                prompt_states[pid] = PromptState(seed, seed_rd(params.seed), params.initial_volatility, 0, False,
                                                 None, seed, seed_rd(params.seed))
        by_user: dict[int, list[Game]] = defaultdict(list)
        for g in day_games:
            by_user[g.user_id].append(g)
        # (user, attempt, prompt, result, player's competition-scope rating and rd at the start of the date)
        settle: dict[int, list[tuple[int, int, float, float, float]]] = defaultdict(list)
        for uid in sorted(by_user):
            _play_user(uid, day, by_user[uid], prompt_by_id, prompt_states, user_scopes[uid], parent_of, params,
                       history, settle)
        for pid in sorted(settle):
            _settle_prompt(pid, day, settle[pid], prompt_states, params)

    scopes = {(uid, scope): st for uid in sorted(user_scopes)
              for scope, st in sorted(user_scopes[uid].items(), key=lambda kv: scope_key(kv[0]))}
    return ReplayResult(scopes, history, dict(sorted(prompt_states.items())))


def _play_user(uid: int, day: date, games: list[Game], prompt_by_id: dict[int, Prompt],
               prompt_states: dict[int, PromptState], states: dict[Scope, State], parent_of, params: Params,
               history: list[HistoryRow], settle: dict) -> None:
    """All of one user's periods on one date. `states` is updated in place."""
    pre = dict(states)  # state before the date; every start of the date derives from it
    starts: dict[Scope, State] = {}

    def start(scope: Scope) -> State:
        if scope in starts:
            return starts[scope]
        if scope in pre:
            st = pre[scope]
            assert st.last_period is not None
            idle = idle_periods(st.last_period, day)
            result = State(st.rating, idle_rd(st.rd, st.volatility, idle, params.initial_rd), st.volatility,
                           st.games, st.last_period)
        elif (parent := parent_of(scope)) is not None:
            p = start(parent)  # the parent's state at the start of the date, never its updated one
            result = State(p.rating, max(params.child_scope_initial_rd, p.rd), params.initial_volatility, 0, None)
        else:
            result = State(params.initial_rating, params.initial_rd, params.initial_volatility, 0, None)
        starts[scope] = result
        return result

    in_period: dict[Scope, list[tuple[int, Opponent]]] = defaultdict(list)
    current: dict[Scope, State] = {}
    attempts: dict[int, list[Game]] = defaultdict(list)
    for g in games:
        attempts[g.attempt].append(g)
    for attempt in sorted(attempts):
        touched: set[Scope] = set()
        for g in sorted(attempts[attempt], key=lambda x: x.prompt_id):
            p = prompt_by_id[g.prompt_id]
            ps = prompt_states[g.prompt_id]
            result = 0.0 if g.unanswered else g.points / params.points_scale
            opp = Opponent(ps.rating, ps.rd, result)
            for scope in prompt_scopes(p):
                in_period[scope].append((g.prompt_id, opp))
                touched.add(scope)
            comp_start = start((COMPETITION, p.competition_id))
            settle[g.prompt_id].append((uid, attempt, result, comp_start.rating, comp_start.rd))
        for scope in sorted(touched, key=scope_key):
            st = start(scope)
            before = current.get(scope, st)
            period = sorted(in_period[scope], key=lambda pair: pair[0])  # stable: attempt order on ties
            opponents = [o for _, o in period]
            out = rate_period(st.rating, st.rd, st.volatility, 0, opponents, params)
            current[scope] = State(out.rating, out.rd, out.volatility, st.games + len(opponents), day)
            score_sum = 0.0
            for o in opponents:
                score_sum += o.score
            history.append(HistoryRow(uid, day, attempt, scope[0], scope[1], before.rating, before.rd,
                                      before.volatility, out.rating, out.rd, out.volatility, len(opponents),
                                      score_sum))
    states.update(current)


def _settle_prompt(pid: int, day: date, rows: list, prompt_states: dict[int, PromptState],
                   params: Params) -> None:
    """Date close: one period for the prompt against its players (by user, then attempt); its score is 1 - result."""
    ps = prompt_states[pid]
    if ps.locked:
        return
    opponents = [Opponent(rating, rd, 1.0 - result)
                 for _, _, result, rating, rd in sorted(rows, key=lambda r: (r[0], r[1]))]
    out = rate_period(ps.rating, ps.rd, ps.volatility, 0, opponents, params)  # prompts get no idle growth
    games = ps.games + len(opponents)
    prompt_states[pid] = PromptState(out.rating, out.rd, out.volatility, games,
                                     games >= params.prompt_lock_answers, day, ps.seed_rating, ps.seed_rd)
