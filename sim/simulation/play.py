"""Plays a simulated world through the real rating engine (sim/rating, in-process) and lays the results out as
(player, date) arrays for the metrics."""

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from rating.core import CENTER, SCALE, Opponent, rate_period  # noqa: F401
from rating.engine import replay
from rating.params import Params
from rating.types import CLUB, SPORT, Game, Prompt

from .answers import sample_prompt
from .oracle import Oracle, solve_true_bk
from .snapshot import Snapshot
from .world import World, prompt_outcomes

D0 = date(2030, 1, 1)
FEATURES = ("log_recall_mass", "ln_answer_count", "top_share", "ln_n_eff")


@dataclass(frozen=True)
class GameArrays:
    player: np.ndarray
    date: np.ndarray  # date index
    prompt: np.ndarray  # snapshot prompt index j
    points: np.ndarray


def sample_games(world: World, snapshot: Snapshot, rng: np.random.Generator) -> GameArrays:
    """Every scheduled (player, date) plays that date's prompts once. Same draw order as runner.sample_dives."""
    players, dates, prompts, points = [], [], [], []
    for j in world.used:
        d = int(world.prompt_date[j])
        who = np.flatnonzero(world.plays[:, d])
        if len(who) == 0:
            continue
        o = prompt_outcomes(world, snapshot, int(j), who, d)
        got = sample_prompt(o, snapshot.points[snapshot.answers(int(j))], rng)
        players.append(who)
        dates.append(np.full(len(who), d))
        prompts.append(np.full(len(who), int(j)))
        points.append(got.points)
    cat = lambda xs, dtype: np.concatenate(xs).astype(dtype) if xs else np.zeros(0, dtype)
    return GameArrays(cat(players, np.int64), cat(dates, np.int64), cat(prompts, np.int64), cat(points, np.float64))


def prompt_features(snapshot: Snapshot, j: int) -> dict[str, float]:
    """The seed features of prompt j, frozen at approval (design section 3)."""
    p = snapshot.prompts[j]
    return {"log_recall_mass": float(p["rarity"]["log_recall_mass"]), "ln_answer_count": float(np.log(p["answer_count"])),
            "top_share": float(p["top_share"]), "ln_n_eff": float(np.log(p["n_eff"]))}


def engine_inputs(world: World, snapshot: Snapshot, games: GameArrays, offsets: dict[str, float]):
    prompts = [Prompt(int(snapshot.prompt_id[j]), int(snapshot.competition_id[j]), snapshot.club_ids[j],
                      prompt_features(snapshot, int(j)), float(offsets.get(snapshot.template[int(j)], 0.0)))
               for j in world.used]
    pid = snapshot.prompt_id
    rated = [Game(int(p), D0 + timedelta(days=int(d)), 1, int(pid[j]), int(pts))
             for p, d, j, pts in zip(games.player, games.date, games.prompt, games.points, strict=True)]
    return prompts, rated


def true_bk_matrix(world: World, snapshot: Snapshot, oracle: Oracle, base_result_mean: np.ndarray, dates: list[int],
                   points_scale: float) -> np.ndarray:
    """(m, len(dates)) true BK of each player with their knowledge on each date, against every played prompt. Players
    without drift do not change, so only the drifters are recomputed per date."""
    base = CENTER + SCALE * solve_true_bk(oracle.v, base_result_mean)
    out = np.tile(base[:, None], (1, len(dates)))
    drifters = np.flatnonzero(world.drift != 0.0)
    if len(drifters) == 0:
        return out
    for k, d in enumerate(dates):
        total = np.zeros(len(drifters))
        for j in world.used:
            o = prompt_outcomes(world, snapshot, int(j), drifters, d)
            total += o.p_valid * (o.q @ snapshot.points[snapshot.answers(int(j))]) / points_scale
        out[drifters, k] = CENTER + SCALE * solve_true_bk(oracle.v, total / len(world.used))
    return out


@dataclass(frozen=True)
class Replayed:
    """The engine's output as (player, date) arrays. Ratings are the sport scope, carried forward between dives."""
    played: np.ndarray  # (m, T) bool
    rating: np.ndarray  # (m, T) nan before the first dive
    rd: np.ndarray  # (m, T) RD as displayed on that date (idle growth applied), nan before the first dive
    rating_before: np.ndarray  # (m, T) rating at the start of a dive, nan on dates not played
    rd_before: np.ndarray  # (m, T) RD at the start of a dive (idle growth applied), nan on dates not played
    club_rows: dict[tuple[int, int], list[tuple[int, int, float, float]]]  # (player, club) -> (date, games, rating, rd)
    prompt_seed: np.ndarray  # (U,) in world.used order
    prompt_rating: np.ndarray  # (U,) after the date-close update
    prompt_rd_seed: np.ndarray  # (U,)
    prompt_games: np.ndarray  # (U,)


def idle_rd_array(rd, volatility, periods, initial_rd):
    """rating.core.idle_rd on arrays."""
    phi = rd / SCALE
    grown = np.sqrt(phi * phi + np.maximum(periods, 0) * volatility * volatility)
    return np.where(periods > 0, np.minimum(grown, initial_rd / SCALE) * SCALE, rd)


def play_world(world: World, snapshot: Snapshot, games: GameArrays, params: Params, offsets: dict[str, float]) -> Replayed:
    prompts, rated = engine_inputs(world, snapshot, games, offsets)
    result = replay(prompts, rated, params)
    m, T = world.n_players, world.n_dates
    played = np.zeros((m, T), dtype=bool)
    r_after, rd_after, vol_after, r_before, rd_before = (np.full((m, T), np.nan) for _ in range(5))
    club_rows: dict[tuple[int, int], list] = {}
    for h in result.history:
        t = (h.date - D0).days
        if h.scope_type == SPORT:
            played[h.user_id, t] = True
            r_after[h.user_id, t], rd_after[h.user_id, t] = h.rating_after, h.rd_after
            vol_after[h.user_id, t], r_before[h.user_id, t] = h.volatility_after, h.rating_before
            rd_before[h.user_id, t] = h.rd_before
        elif h.scope_type == CLUB:
            club_rows.setdefault((h.user_id, h.scope_id), []).append((t, h.games, h.rating_after, h.rd_after))
    idx = np.where(played, np.arange(T)[None, :], -1)
    last = np.maximum.accumulate(idx, axis=1)
    seen = last >= 0
    safe = np.maximum(last, 0)
    rating = np.where(seen, np.take_along_axis(r_after, safe, axis=1), np.nan)
    rd_last = np.take_along_axis(rd_after, safe, axis=1)
    vol_last = np.take_along_axis(vol_after, safe, axis=1)
    idle = np.arange(T)[None, :] - last - 1
    rd = np.where(seen, idle_rd_array(rd_last, vol_last, idle, params.initial_rd), np.nan)
    ps = [result.prompts[int(snapshot.prompt_id[j])] for j in world.used]
    return Replayed(played, rating, rd, r_before, rd_before, club_rows,
                    np.array([p.seed_rating for p in ps]), np.array([p.rating for p in ps]),
                    np.array([p.seed_rd for p in ps]), np.array([p.games for p in ps]))
