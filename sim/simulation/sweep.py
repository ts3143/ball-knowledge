"""The Glicko grid: combinations, the objective, feasibility and the choice of winner and runner-up."""

import copy
import itertools
from dataclasses import dataclass

import numpy as np

WORLD_KEYS = ("model-true", "model-wrong")


def combos(cfg: dict) -> list[tuple[float, float, float]]:
    return list(itertools.product(cfg["tau"], cfg["child_scope_initial_rd"], cfg["game_weight"]))


def label(combo: tuple[float, float, float]) -> str:
    tau, crd, w = combo
    return f"tau{tau:g}-crd{crd:g}-w{w:g}"


def with_glicko(tunables: dict, combo: tuple[float, float, float]) -> dict:
    out = copy.deepcopy(tunables)
    out["glicko"]["tau"], out["glicko"]["child_scope_initial_rd"], out["glicko"]["game_weight"] = combo
    return out


def get(agg: dict, key: str, stat: str = "mean") -> float:
    return agg[key][stat] if key in agg else float("nan")


def objective(agg: dict, first_date: int) -> float:
    """Mean estimation Spearman over the dates first_date..last (every cohort together)."""
    vals = [v["mean"] for k, v in agg.items() if k.startswith("est.all.") and int(k.rsplit(".", 1)[1]) >= first_date]
    return float(np.mean(vals)) if vals else float("nan")


def summary_row(agg: dict, first_date: int) -> dict:
    margins = [get(agg, f"est.irregular.{d:03d}") - get(agg, f"base.irregular.{d:03d}") for d in (14, 28, 61)]
    return {"objective": objective(agg, first_date), "cov2": get(agg, "rd.cov2.all"), "cov1": get(agg, "rd.cov1.all"),
            "misplaced_ratio": get(agg, "rd.misplaced_ratio"), "placement": get(agg, "placement.median"),
            "inflation": get(agg, "inflation.per30"), "irregular_margin": float(np.mean(margins)),
            "est_daily_14": get(agg, "est.daily.014"), "est_irregular_28": get(agg, "est.irregular.028"),
            "e2e_28": get(agg, "e2e.all.028"), "club_spearman_10": get(agg, "clubs.n10.spearman"), "club_delta_10": get(agg, "clubs.n10.delta_spearman"),
            "club_cov2_10": get(agg, "clubs.n10.cov2"), "club_rd_10": get(agg, "clubs.n10.rd")}


@dataclass(frozen=True)
class Entry:
    combo: tuple[float, float, float]
    rows: dict  # world -> summary_row
    feasible: bool
    score: float  # mean objective over the worlds
    distance: float  # how far outside the coverage band, worst world (0 when feasible)
    diverged: bool = False
    club_score: float = 0.0  # mean over the worlds of the within-club Spearman, at 10 prompts, of (club minus overall rating) against the true difference: what a club rating adds; reported, not used to choose


def entries(per_world_agg: dict[str, dict[str, dict]], cfg: dict) -> list[Entry]:
    """per_world_agg[world][label] = aggregate of that combination. Sorted best first: feasible by score, then infeasible by
    distance from the band."""
    out = []
    for combo in combos(cfg):
        rows = {w: summary_row(per_world_agg[w][label(combo)], cfg["objective_first_date"]) for w in WORLD_KEYS}
        diverged = any(get(per_world_agg[w][label(combo)], "engine.diverged") > 0 for w in WORLD_KEYS)
        if diverged or any(np.isnan(r["cov2"]) for r in rows.values()):
            dist = float("inf")  # a diverged or undefined combination is never feasible
        else:
            dist = max(max(0.0, cfg["coverage_min"] - r["cov2"], r["cov2"] - cfg["coverage_max"]) for r in rows.values())
        score = float(np.mean([r["objective"] for r in rows.values()]))
        score = float("-inf") if np.isnan(score) else score
        club = [r["club_delta_10"] for r in rows.values() if not np.isnan(r["club_delta_10"])]
        out.append(Entry(combo, rows, dist == 0.0, score, dist, diverged, float(np.mean(club)) if club else 0.0))
    return sorted(out, key=lambda e: (not e.feasible, e.distance if not e.feasible else -e.score, -e.score))


def simplicity(combo: tuple[float, float, float], cfg: dict) -> tuple:
    """Tie-break key, smaller is simpler: game weight nearest 1, tau inside Glickman's range, tau nearest 0.5, child RD nearest 250."""
    tau, crd, w = combo
    lo, hi = cfg["glickman_tau"]
    return (abs(w - 1.0), 0 if lo <= tau <= hi else 1, abs(tau - 0.5), abs(crd - 250.0))


def pick(ranked: list[Entry], cfg: dict, n: int = 2) -> list[Entry]:
    """The best n entries, one at a time: among the remaining entries of the best class (feasible, else nearest the band) whose
    score is within tie_tolerance of the best, the simplest (game weight, then tau), then the child RD nearest 250 (the sport-scope objective cannot tell child RDs apart). Needs ranked from `entries`."""
    remaining = list(ranked)
    chosen = []
    while remaining and len(chosen) < n:
        head = remaining[0]
        pool = [e for e in remaining if e.feasible == head.feasible and (e.feasible or e.distance == head.distance)]
        best = max(e.score for e in pool)
        tied = [e for e in pool if e.score >= best - cfg["tie_tolerance"]] if head.feasible else [head]
        winner = min(tied, key=lambda e: simplicity(e.combo, cfg))
        chosen.append(winner)
        remaining.remove(winner)
    return chosen
