"""Runs scenarios (many seeds, in worker processes), aggregates their metrics and evaluates T's pass bars.
Every task is a pure function of its inputs, so results do not depend on the number of workers."""

import functools
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from rating.core import VolatilityError
from rating.params import Params

from .metrics import REPORT_DATES, run_metrics
from .oracle import fit_oracle, spearman
from .play import play_world, sample_games, true_bk_matrix
from .runner import club_true_bk
from .snapshot import load_snapshot
from .tiers import retier
from .world import build_world, expected_matrices, streams


@dataclass(frozen=True)
class Task:
    snapshot_path: str
    world_name: str
    scenario: str
    seed: int
    cfg: dict  # world config with the scenario's players and dates
    prompts_per_dive: int
    points_scale: float  # points that make a result of 1.0
    configs: tuple  # ((label, tunables), ...): each is replayed on the same games; empty = oracle truth only
    tier_variant: object = None  # a tiers.TierVariant: re-tier the snapshot before anything else
    analysis: object = None  # what to report besides the pass bars (metrics.DEFAULT_ANALYSIS keys)


@functools.lru_cache(maxsize=2)
def cached_snapshot(path: str):
    return load_snapshot(Path(path))


def run_task(task: Task) -> dict:
    snapshot = cached_snapshot(task.snapshot_path)
    if task.tier_variant is not None:
        snapshot, _ = retier(snapshot, task.tier_variant)
    full = task.scenario == "population"
    ppd, scale = task.prompts_per_dive, task.points_scale
    world = build_world(snapshot, task.cfg, ppd, task.seed, task.world_name)
    expected = expected_matrices(world, snapshot, scale)
    oracle = fit_oracle(expected.result)
    tier_points = np.array([snapshot.points[snapshot.tier == k].mean() if (snapshot.tier == k).any() else 0.0 for k in range(1, 6)])
    by_tier = expected.tier_mass.sum(axis=(0, 1)) * tier_points
    ykb_share = float(by_tier[4] / by_tier.sum())  # share of all expected points that comes from You Know Ball answers
    out = {"seed": task.seed, "prompt_ids": [int(snapshot.prompt_id[j]) for j in world.used],
           "true_rating": [float(x) for x in oracle.prompt_rating], "metrics": {}}
    if not task.configs:
        return out
    games = sample_games(world, snapshot, streams(task.seed)["answers"])
    T = world.n_dates
    dates = list(range(T)) if full else [d - 1 for d in REPORT_DATES if d <= T]
    true_bk = true_bk_matrix(world, snapshot, oracle, expected.result.mean(axis=1), dates, scale)
    clubs, club_true = club_true_bk(world, snapshot, oracle, expected) if full else ([], None)


    validity = spearman(oracle.player_rating, world.theta)
    for label, tunables in task.configs:
        params = Params.from_tunables(tunables)
        offsets = tunables["glicko"]["prompt_seed"].get("template_offsets", {})
        try:
            rep = play_world(world, snapshot, games, params, offsets)
        except (ArithmeticError, VolatilityError) as e:
            # A parameter combination whose volatility or ratings blow up (a game weight far above 1 with a large tau does this):
            # recorded, never hidden, and such a combination is infeasible.
            out["metrics"][label] = {"engine.diverged": 1.0}
            out.setdefault("errors", {})[label] = f"{type(e).__name__}: {e}"
            continue
        out["metrics"][label] = run_metrics(world, snapshot, oracle.prompt_rating, true_bk, dates, validity, oracle.rmse,
                                            club_true, clubs, games, rep, tunables, full, task.analysis)
        out["metrics"][label]["engine.diverged"] = 0.0
        out["metrics"][label]["tiers.ykb_points_share"] = ykb_share
    return out


def run_tasks(tasks: list[Task], workers: int) -> list[dict]:
    if workers <= 1 or len(tasks) == 1:
        return [run_task(t) for t in tasks]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run_task, tasks, chunksize=1))


def aggregate(runs: list[dict]) -> dict[str, dict]:
    """Per metric across seeds: mean, min, max, median, p10, p90 and the number of seeds where it is defined."""
    out = {}
    for key in sorted({k for r in runs for k in r}):  # a template can be missing from a small scenario's prompts
        v = np.array([r.get(key, float("nan")) for r in runs], dtype=float)
        ok = v[~np.isnan(v)]
        if len(ok) == 0:
            out[key] = {"mean": float("nan"), "min": float("nan"), "max": float("nan"), "median": float("nan"),
                        "p10": float("nan"), "p90": float("nan"), "n": 0}
        else:
            out[key] = {"mean": float(ok.mean()), "min": float(ok.min()), "max": float(ok.max()), "median": float(np.median(ok)),
                        "p10": float(np.percentile(ok, 10)), "p90": float(np.percentile(ok, 90)), "n": len(ok)}
    return out


def per_seed(runs: list[dict], key: str) -> list[float]:
    return [r[key] for r in runs]


def mean_of(agg: dict, key: str, stat: str = "mean") -> float:
    return agg[key][stat] if key in agg else float("nan")


# ---------------------------------------------------------------------------
# T's pass bars
# ---------------------------------------------------------------------------

BAR_NAMES = (
    "Validity: oracle BK vs true skill >= 0.85",
    "Estimation, daily players, date 14 >= 0.90",
    "Estimation, irregular players, date 28 >= 0.85",
    "Beats the mean-points baseline, irregular players (dates 14, 28, 61)",
    "Median placement <= 10 dives",
    "2 RD coverage between 90% and 98%",
    "Uncertain players misplaced >= 2x as often as settled ones",
    "Inflation <= 20 points per 30 dates",
    "Testers: median Spearman with true skill >= 0.8 at date 28",
)


def evaluate_bars(pop: dict | None, testers: dict | None) -> list[dict]:
    """One dict per bar: name, value (text), threshold, passed (True/False/None when the scenario was not run)."""
    rows = []

    def row(i, value, text, ok):
        rows.append({"bar": BAR_NAMES[i], "value": value, "text": text, "passed": None if np.isnan(value) else bool(ok)})

    nan = float("nan")
    p = pop or {}
    v = mean_of(p, "validity.oracle") if pop else nan
    row(0, v, f"{v:.3f}", v >= 0.85)
    v = mean_of(p, "est.daily.014") if pop else nan
    row(1, v, f"{v:.3f}", v >= 0.90)
    v = mean_of(p, "est.irregular.028") if pop else nan
    row(2, v, f"{v:.3f}", v >= 0.85)
    gains = [mean_of(p, f"est.irregular.{d:03d}") - mean_of(p, f"base.irregular.{d:03d}") for d in (14, 28, 61)] if pop else [nan]
    v = float(np.mean(gains))
    row(3, v, "BK minus baseline: " + ", ".join(f"{g:+.3f}" for g in gains), v > 0)
    v = mean_of(p, "placement.median") if pop else nan
    share = mean_of(p, "placement.share_placed") if pop else nan
    row(4, v, f"{v:.0f} dives ({share:.0%} of players placed)" if v < 999 else f"not placed ({share:.0%} placed)", v <= 10)
    v = mean_of(p, "rd.cov2.all") if pop else nan
    row(5, v, f"{v:.1%} (within 1 RD: {mean_of(p, 'rd.cov1.all') if pop else nan:.1%})", 0.90 <= v <= 0.98)
    v = mean_of(p, "rd.misplaced_ratio") if pop else nan
    row(6, v, f"{v:.2f}x", v >= 2.0)
    v = mean_of(p, "inflation.per30") if pop else nan
    row(7, v, f"{v:+.1f}", abs(v) <= 20)
    t = testers or {}
    v = mean_of(t, "e2e.all.028", "median") if testers else nan
    est = mean_of(t, "est.all.028", "median") if testers else nan
    row(8, v, f"{v:.3f} (vs true BK: {est:.3f})", v >= 0.8)
    return rows


def diagnostics(pop: dict | None, label: str) -> list[tuple[str, str, str]]:
    """Findings as (code, text, kind). kind 'engine' = would point at wrong engine semantics whatever the tuning;
    'untuned' = a parameter or the seed map is off. The code lets the report tell whether the proposed seed removes a finding."""
    out = []
    if not pop:
        return out
    curve = [mean_of(pop, f"est.all.{d:03d}") for d in (14, 28, 61)]
    if not np.isnan(curve).any() and curve[2] < curve[0] - 0.02:
        out.append(("worsens", (f"{label}: estimation gets worse with more dives ({curve[0]:.3f} at date 14, {curve[2]:.3f} at date 61); "
                    "ratings should sharpen with data."), "engine"))
    ratio = mean_of(pop, "rd.misplaced_ratio")
    if not np.isnan(ratio) and ratio < 1.0:
        out.append(("rd_blind", (f"{label}: players marked uncertain are misplaced no more often than settled ones ({ratio:.2f}x); RD carries "
                    "no information about how wrong a rating is."), "engine"))
    cov2 = mean_of(pop, "rd.cov2.all")
    if cov2 < 0.80:
        out.append(("rd_small", f"{label}: only {cov2:.0%} of errors are within 2 RD; RD is much too small.", "engine"))
    if cov2 > 0.98:
        out.append(("rd_large", (f"{label}: {cov2:.1%} of errors are within 2 RD and {mean_of(pop, 'rd.cov1.all'):.0%} within 1 RD; RD is larger "
                    "than the errors warrant. The design expected this at game_weight 1 (fractional results carry about a third of the "
                    "variance the win/loss model assumes); chunk 4 sweeps game_weight."), "untuned"))
    slope = mean_of(pop, "scale.slope.last")
    if not np.isnan(slope) and not 0.9 <= slope <= 1.1:
        out.append(("scale", (f"{label}: BK spread is {slope:.2f}x the true spread and the median is {mean_of(pop, 'scale.level_offset.last'):.0f} "
                    "points off: prompts all start at 1500 with RD 350, which is not where they are. The seed map is meant to fix this."), "untuned"))
    gains = [mean_of(pop, f"est.irregular.{d:03d}") - mean_of(pop, f"base.irregular.{d:03d}") for d in (14, 28, 61)]
    if max(gains) < 0:
        out.append(("baseline", (f"{label}: BK is below 'mean points so far' at every report date for irregular players "
                    f"({', '.join(f'{g:+.3f}' for g in gains)}). Everyone plays the same prompts on a date, so mean points is a strong "
                    "baseline; with prompts all at 1500 BK cannot do better."), "untuned"))
    if abs(mean_of(pop, "inflation.per30")) > 20:
        out.append(("inflation", (f"{label}: the mean BK of non-drifting players moves {mean_of(pop, 'inflation.per30'):+.0f} points in 30 "
                    "dates; that is the rating settling to the prompts' level, not a trend."), "untuned"))
    return out
