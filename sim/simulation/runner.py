"""Builds a world, samples its games, fits the oracle and writes the run directory (config, metrics, report, CSVs, PNGs)."""

import csv
import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from rating import ENGINE_VERSION
from rating.core import CENTER, SCALE
from rating.params import load_tunables

from .answers import VALID, sample_prompt
from .oracle import Oracle, fit_oracle, solve_true_bk, spearman
from .snapshot import SIM_ROOT, Snapshot, check_retiering, load_snapshot
from .world import (
    COHORTS,
    ExpectedMatrices,
    World,
    build_world,
    expected_matrices,
    load_world_config,
    prompt_outcomes,
    streams,
)

OUTPUT_DIR = SIM_ROOT / "output"
PERCENTILES = (10, 25, 50, 75, 90, 99)
PERCENTILE_WINDOW = 2.5  # a band is the players within this many percentile points of the percentile
REPORT_DATES = (7, 14, 28, 61)  # 1-based dive dates the design reports at
TIER_NAMES = ("Bandwagon", "Casual", "Real One", "Deep Cut", "You Know Ball")
EXAMPLE_PROMPTS = 3
ROUND = 8


def clean(obj):
    """JSON-ready, floats rounded, so metrics.json is byte-identical for identical inputs."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        return None if np.isnan(obj) else round(float(obj), ROUND)  # NaN is not JSON
    if isinstance(obj, np.integer):
        return int(obj)
    return obj


def git_commit() -> str:
    out = subprocess.run(["git", "-C", str(SIM_ROOT.parent), "rev-parse", "--short", "HEAD"], capture_output=True,
                         text=True, check=False)
    return out.stdout.strip() or "unknown"


@dataclass(frozen=True)
class WorldRun:
    world: World
    expected: ExpectedMatrices
    oracle: Oracle
    sampled_dives: np.ndarray  # (m, T) sampled dive score, nan where the player did not play
    true_bk: dict[int, np.ndarray]  # date index -> (m,) true BK
    club_true_bk: np.ndarray  # (m, n_clubs)
    clubs: list[int]


def sample_dives(world: World, snapshot: Snapshot, rng: np.random.Generator) -> np.ndarray:
    """Plays every scheduled (player, date) once, prompt by prompt in play order; returns the dive scores."""
    dives = np.where(world.plays, 0.0, np.nan)
    for j in world.used:
        date = int(world.prompt_date[j])
        players = np.flatnonzero(world.plays[:, date])
        if len(players) == 0:
            continue
        o = prompt_outcomes(world, snapshot, int(j), players, date)
        dives[players, date] += sample_prompt(o, snapshot.points[snapshot.answers(int(j))], rng).points
    return dives


def true_bk_at(world: World, snapshot: Snapshot, oracle: Oracle, expected: ExpectedMatrices, date: int,
               points_scale: float) -> np.ndarray:
    """True BK of every player with their knowledge on `date`, against all used prompts. Players without drift do
    not change, so only the drifters are recomputed."""
    target = expected.result.mean(axis=1)
    drifters = np.flatnonzero(world.drift != 0.0)
    if len(drifters):
        total = np.zeros(len(drifters))
        for j in world.used:
            o = prompt_outcomes(world, snapshot, int(j), drifters, date)
            total += (o.p_valid * (o.q @ snapshot.points[snapshot.answers(int(j))])) / points_scale
        target = target.copy()
        target[drifters] = total / len(world.used)
    return CENTER + SCALE * solve_true_bk(oracle.v, target)


def club_true_bk(world: World, snapshot: Snapshot, oracle: Oracle, expected: ExpectedMatrices) -> tuple[list[int], np.ndarray]:
    used = list(world.used)
    clubs = sorted({c for j in used for c in snapshot.club_ids[j]})
    out = np.zeros((world.n_players, len(clubs)))
    for k, c in enumerate(clubs):
        cols = [u for u, j in enumerate(used) if c in snapshot.club_ids[j]]
        out[:, k] = CENTER + SCALE * solve_true_bk(oracle.v[cols], expected.result[:, cols].mean(axis=1))
    return clubs, out


def run_world(snapshot: Snapshot, cfg: dict, tunables: dict, seed: int, world_name: str) -> WorldRun:
    ppd = int(tunables["dive"]["prompts_per_dive"])
    scale = float(max(tunables["tiers"]["points"]))
    world = build_world(snapshot, cfg, ppd, seed, world_name)
    expected = expected_matrices(world, snapshot, scale)
    oracle = fit_oracle(expected.result)
    dives = sample_dives(world, snapshot, streams(seed)["answers"])
    dates = sorted({0} | {min(d, world.n_dates) - 1 for d in REPORT_DATES})
    true_bk = {d: true_bk_at(world, snapshot, oracle, expected, d, scale) for d in dates}
    clubs, club_bk = club_true_bk(world, snapshot, oracle, expected)
    return WorldRun(world, expected, oracle, dives, true_bk, club_bk, clubs)


def band(theta: np.ndarray, percentile: float) -> np.ndarray:
    rank = (np.argsort(np.argsort(theta)) + 0.5) / len(theta) * 100.0
    lo, hi = percentile - PERCENTILE_WINDOW, percentile + PERCENTILE_WINDOW
    if hi > 100:
        lo -= hi - 100
    return np.flatnonzero((rank >= lo) & (rank <= hi))


def percentile_rows(run: WorldRun, ppd: int, scale: float) -> list[dict]:
    w, ex = run.world, run.expected
    rows = []
    for p in PERCENTILES:
        idx = band(w.theta, p)
        valid_mass = ex.valid[idx].sum()
        sampled = run.sampled_dives[idx]
        sampled = sampled[~np.isnan(sampled)]
        rows.append({
            "percentile": p, "players": len(idx), "theta": float(w.theta[idx].mean()),
            "valid_rate": float(ex.valid[idx].mean()), "blank_rate": float(ex.blank[idx].mean()),
            "wrong_rate": float(ex.wrong[idx].mean()), "mean_points_per_prompt": float(ex.points[idx].mean()),
            "dive_score": float(ppd * ex.points[idx].mean()), "dive_score_out_of": int(ppd * scale),
            "sampled_dive_median": float(np.median(sampled)) if len(sampled) else float("nan"),
            "sampled_dive_p10": float(np.percentile(sampled, 10)) if len(sampled) else float("nan"),
            "sampled_dive_p90": float(np.percentile(sampled, 90)) if len(sampled) else float("nan"),
            "tier_mix": [float(x) for x in ex.tier_mass[idx].sum(axis=(0, 1)) / valid_mass]})
    return rows


def example_prompt_indices(snapshot: Snapshot, world: World) -> list[int]:
    """The median-sized used prompt of each of the three most common templates among the used prompts."""
    used = [int(j) for j in world.used]
    by_template: dict[str, list[int]] = {}
    for j in used:
        by_template.setdefault(snapshot.template[j], []).append(j)
    top = sorted(by_template, key=lambda t: (-len(by_template[t]), t))[:EXAMPLE_PROMPTS]
    out = []
    for t in top:
        js = sorted(by_template[t], key=lambda j: (snapshot.offsets[j + 1] - snapshot.offsets[j], j))
        out.append(js[len(js) // 2])
    return out


def share_rows(run: WorldRun, snapshot: Snapshot, j: int, rng: np.random.Generator) -> dict:
    """Predicted vs simulated pick shares of prompt j with every player answering it on its date."""
    w = run.world
    everyone = np.arange(w.n_players)
    o = prompt_outcomes(w, snapshot, j, everyone, max(int(w.prompt_date[j]), 0))
    sl = snapshot.answers(j)
    expected_picks = (o.p_valid[:, None] * o.q).sum(axis=0)
    simulated = expected_picks / expected_picks.sum()
    sampled = sample_prompt(o, snapshot.points[sl], rng)
    counts = np.bincount(sampled.answer[sampled.outcome == VALID], minlength=sl.stop - sl.start)
    return {"prompt_id": int(snapshot.prompt_id[j]), "template": snapshot.template[j], "answers": sl.stop - sl.start,
            "valid_picks": int(counts.sum()), "predicted": snapshot.predicted_share[sl], "simulated": simulated,
            "sampled": counts / max(counts.sum(), 1), "tier": snapshot.tier[sl]}


def template_rows(run: WorldRun, snapshot: Snapshot) -> list[dict]:
    rating = run.oracle.prompt_rating
    by: dict[str, list[float]] = {}
    for u, j in enumerate(run.world.used):
        by.setdefault(snapshot.template[int(j)], []).append(float(rating[u]))
    rows = []
    for t in sorted(by):
        r = np.array(by[t])
        rows.append({"template": t, "prompts": len(r), "min": float(r.min()), "p25": float(np.percentile(r, 25)),
                     "median": float(np.median(r)), "p75": float(np.percentile(r, 75)), "max": float(r.max())})
    return rows


def compute_metrics(run: WorldRun, snapshot: Snapshot, tunables: dict, seed: int, world_name: str,
                    examples: list[dict]) -> dict:
    w, ppd = run.world, int(tunables["dive"]["prompts_per_dive"])
    scale = float(max(tunables["tiers"]["points"]))
    rating = run.oracle.prompt_rating
    first, last = min(run.true_bk), max(run.true_bk)
    drifters = w.drift != 0.0
    return {
        "world": world_name, "seed": seed, "c0": w.c0,
        "validity_spearman_oracle_vs_theta": spearman(run.oracle.player_rating, w.theta),
        "oracle_fit_rmse": run.oracle.rmse, "oracle_iterations": run.oracle.iterations,
        "players": w.n_players, "dates": w.n_dates, "prompts_used": len(w.used), "prompts_in_snapshot": snapshot.n_prompts,
        "cohort_sizes": {c: int((w.cohort == k).sum()) for k, c in enumerate(COHORTS)},
        "favourites": {str(n): sum(len(f) == n for f in w.favourites) for n in (0, 1, 2)},
        "drifters": int(drifters.sum()),
        "prompt_rating": {"min": rating.min(), "p10": np.percentile(rating, 10), "median": np.median(rating),
                          "p90": np.percentile(rating, 90), "max": rating.max()},
        "player_rating": {"p5": np.percentile(run.oracle.player_rating, 5), "p50": np.median(run.oracle.player_rating),
                          "p95": np.percentile(run.oracle.player_rating, 95)},
        "true_bk_drift": {"from_date": first + 1, "to_date": last + 1,
                          "mean_abs_change_of_drifters": float(np.abs(run.true_bk[last] - run.true_bk[first])[drifters].mean())
                          if drifters.any() else 0.0},
        "true_bk_dates": sorted(d + 1 for d in run.true_bk),
        "clubs": {"count": len(run.clubs), "median_prompts": float(np.median([sum(c in snapshot.club_ids[int(j)] for j in w.used)
                                                                                for c in run.clubs]))},
        "by_percentile": percentile_rows(run, ppd, scale),
        "by_template": template_rows(run, snapshot),
        "example_prompts": [{"prompt_id": e["prompt_id"], "template": e["template"], "answers": e["answers"],
                             "valid_picks": e["valid_picks"],
                             "spearman_predicted_vs_simulated": spearman(e["predicted"], e["simulated"])} for e in examples],
    }


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in rows:
            writer.writerow([round(x, ROUND) if isinstance(x, float) else x for x in r])


def charts(out: Path, metrics: dict, examples: list[dict], run: WorldRun, snapshot: Snapshot) -> None:
    rows = metrics["by_percentile"]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot([r["percentile"] for r in rows], [r["dive_score"] for r in rows], "o-", label="expected dive score")
    ax.fill_between([r["percentile"] for r in rows], [r["sampled_dive_p10"] for r in rows],
                    [r["sampled_dive_p90"] for r in rows], alpha=0.2, label="sampled dives, p10 to p90")
    ax.set_xlabel("knowledge percentile (theta)")
    ax.set_ylabel(f"dive score (of {rows[0]['dive_score_out_of']})")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "dive_score_by_percentile.png", dpi=120)
    plt.close(fig)

    fig, axes = plt.subplots(1, len(examples), figsize=(4 * len(examples), 4), squeeze=False)
    for ax, e in zip(axes[0], examples):
        ax.loglog(e["predicted"], np.maximum(e["simulated"], 1e-6), ".", label="expected")
        ax.loglog(e["predicted"], np.maximum(e["sampled"], 1e-6), "x", alpha=0.5, label="sampled")
        lim = [1e-6, 1]
        ax.plot(lim, lim, "k--", lw=0.8)
        ax.set_title(f"prompt {e['prompt_id']}")
        ax.set_xlabel("predicted share")
        ax.set_ylabel("simulated share")
    axes[0][0].legend()
    fig.tight_layout()
    fig.savefig(out / "pick_shares.png", dpi=120)
    plt.close(fig)

    by: dict[str, list[float]] = {}
    for u, j in enumerate(run.world.used):
        by.setdefault(snapshot.template[int(j)], []).append(float(run.oracle.prompt_rating[u]))
    names = sorted(by)
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.boxplot([by[n] for n in names], tick_labels=names)
    ax.set_ylabel("true prompt rating")
    ax.tick_params(axis="x", rotation=30)
    fig.tight_layout()
    fig.savefig(out / "prompt_ratings_by_template.png", dpi=120)
    plt.close(fig)


def pct(x: float, digits: int = 1) -> str:
    return f"{100 * x:.{digits}f}%"


def report_markdown(metrics: dict, examples: list[dict], cfg: dict, tunables: dict, snapshot: Snapshot) -> str:
    m, rows = metrics, metrics["by_percentile"]
    out_of = rows[0]["dive_score_out_of"]
    wrong_world = m["world"] == "model-wrong"
    lines = [
        f"# World sanity report: {m['world']}, seed {m['seed']}", "",
        ("This is the simulated world the rating system will be tested on (step 4). Nothing here involves the rating "
        "engine yet: it only checks that the fake players answer the way real ones plausibly would."), "",
        "## What was simulated", "",
        (f"- {m['players']} fake players, {m['dates']} dive dates, {tunables['dive']['prompts_per_dive']} prompts per dive; "
        f"{m['prompts_used']} of the {m['prompts_in_snapshot']} prompts in the snapshot are played, in a seeded order."),
        (f"- Snapshot: exported {snapshot.header['export_date']} at commit {snapshot.header['git_commit']}, model "
        f"{snapshot.header['model_version']}, sha256 `{snapshot.header['prompts_sha256']}`."),
        ("- Each player has a knowledge level (theta, a normal with spread "
        f"{cfg['theta_sd']}), 0 to 2 favourite clubs that lift their knowledge on those clubs' prompts, a strategy "
        "(obvious answers versus rare ones; experts lean rare) and, for "
        f"{pct(cfg['drift_share'], 0)} of players, a drift in knowledge over the season."),
        (f"- A player knows a given answer with a chance that rises with their knowledge and with how obvious the answer is "
        f"(its stored rarity score). One constant, c0 = {m['c0']:.3f}, sets the overall difficulty so that a median "
        f"player (theta 0, no favourite club) gives a valid answer on {pct(cfg['median_valid_rate'], 0)} of prompts."),
        ("- The answer they give is chosen among the ones they know, weighted by their strategy; some answers are simply "
        "wrong, and the rest are blank. Wrong and blank score 0."),
    ]
    if wrong_world:
        lines.append(f"- Model-wrong world: what players find obvious is not what the rarity model predicted. Every answer's "
                     f"score is shifted by random noise (spread {cfg['model_wrong']['answer_noise_sd']}) plus one shift per "
                     f"template (spread {cfg['model_wrong']['template_noise_sd']}). Tiers and points stay as stored.")
    lines += ["", "## Headline numbers", "",
              (f"- **Validity** (rank correlation of the oracle's true BK with knowledge theta): "
              f"**{m['validity_spearman_oracle_vs_theta']:.3f}**. The oracle is the best possible rating given how players "
              f"really answer, so this is the ceiling for any rating built from these prompts and points."),
              (f"- **Oracle fit error** (RMSE of the logistic rating model against the true expected results, in result units "
              f"where 1.0 is a perfect answer): **{m['oracle_fit_rmse']:.4f}**. Small means a single rating per player and "
              f"prompt explains the world well; Glicko assumes that."),
              f"- **c0** = {m['c0']:.3f}.",
              (f"- True player ratings (median pinned at 1500): p5 {m['player_rating']['p5']:.0f}, p50 "
              f"{m['player_rating']['p50']:.0f}, p95 {m['player_rating']['p95']:.0f}. True prompt ratings: "
              f"min {m['prompt_rating']['min']:.0f}, median {m['prompt_rating']['median']:.0f}, max {m['prompt_rating']['max']:.0f}."),
              "", "## How players at each knowledge percentile do", "",
              (f"Players within {PERCENTILE_WINDOW} percentile points of each level (by theta), averaged over all played prompts. "
              f"Rates are shares of prompts. The dive score is out of {out_of}: 'expected' is the closed-form average, "
              "'sampled' is the median and the p10 to p90 range of the dives the simulation actually played."), "",
              "| percentile | players | valid | blank | wrong | mean points per prompt | expected dive score | sampled dive (median, p10 to p90) |",
              "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| p{r['percentile']} | {r['players']} | {pct(r['valid_rate'])} | {pct(r['blank_rate'])} | "
                     f"{pct(r['wrong_rate'])} | {r['mean_points_per_prompt']:.1f} | {r['dive_score']:.0f} | "
                     f"{r['sampled_dive_median']:.0f} ({r['sampled_dive_p10']:.0f} to {r['sampled_dive_p90']:.0f}) |")
    lines += ["", "Tier mix of the valid answers each group gives (share of their valid answers in each tier):", "",
              "| percentile | " + " | ".join(TIER_NAMES) + " |", "|---|" + "---|" * len(TIER_NAMES)]
    for r in rows:
        lines.append(f"| p{r['percentile']} | " + " | ".join(pct(x) for x in r["tier_mix"]) + " |")
    lines += ["", "![Dive score by percentile](dive_score_by_percentile.png)", "",
              "## Simulated pick shares against the stored predictions", "",
              ("Three example prompts (the median-sized one of the three most common templates). For each, every one of the "
              f"{m['players']} players answers it; 'simulated' is the share of valid picks the answer model gives it "
              "(exact average), 'sampled' is what one random run picked. The predicted share is what the rarity model "
              "stored. Shown: the 8 most predicted answers and the 4 rarest."), ""]
    for e, summary in zip(examples, m["example_prompts"]):
        order = np.argsort(-e["predicted"], kind="mergesort")
        show = list(order[:8]) + list(order[-4:]) if len(order) > 12 else list(order)
        lines += [(f"**Prompt {e['prompt_id']}** ({e['template']}, {e['answers']} answers, {e['valid_picks']} valid picks "
                  f"sampled, rank correlation predicted vs simulated {summary['spearman_predicted_vs_simulated']:.3f})"), "",
                  "| rank | tier | predicted | simulated | sampled |", "|---|---|---|---|---|"]
        for k in show:
            lines.append(f"| {k_rank(order, k)} | {TIER_NAMES[int(e['tier'][k]) - 1]} | {pct(e['predicted'][k], 2)} | "
                         f"{pct(e['simulated'][k], 2)} | {pct(e['sampled'][k], 2)} |")
        lines.append("")
    lines += ["![Pick shares](pick_shares.png)", "", "## True prompt ratings by template", "",
              "The oracle's rating of each played prompt (higher = harder: players score less). One row per template.", "",
              "| template | prompts | min | p25 | median | p75 | max |", "|---|---|---|---|---|---|---|"]
    for r in m["by_template"]:
        lines.append(f"| {r['template']} | {r['prompts']} | {r['min']:.0f} | {r['p25']:.0f} | {r['median']:.0f} | "
                     f"{r['p75']:.0f} | {r['max']:.0f} |")
    c = m["cohort_sizes"]
    lines += ["", "![Prompt ratings by template](prompt_ratings_by_template.png)", "", "## Cohorts and drift", "",
              "Schedule cohorts: " + ", ".join(f"{k} {v}" for k, v in c.items()) + f" (of {m['players']}). "
              f"Favourite clubs: " + ", ".join(f"{n} club{'s' if n != '1' else ''}: {v}" for n, v in m["favourites"].items()) + ".",
              (f"{m['drifters']} players drift. Between dive {m['true_bk_drift']['from_date']} and dive "
              f"{m['true_bk_drift']['to_date']} their true BK moves by {m['true_bk_drift']['mean_abs_change_of_drifters']:.0f} "
              f"points on average (absolute change). Club true ratings exist for {m['clubs']['count']} clubs "
              f"(median {m['clubs']['median_prompts']:.0f} played prompts per club)."), "",
              ("Files next to this report: `metrics.json`, `config.resolved.json`, `by_percentile.csv`, `pick_shares.csv`, "
              "`prompt_ratings.csv`, and the three PNGs.")]
    return "\n".join(lines) + "\n"


def k_rank(order: np.ndarray, k: int) -> int:
    return int(np.flatnonzero(order == k)[0]) + 1


def run_dir_name(timestamp: str, scenario: str, config: dict) -> str:
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:8]
    return f"{timestamp}-{scenario}-{digest}"


def world_report(snapshot_path: Path, world_name: str, seed: int, overrides: list[str], output_dir: Path = OUTPUT_DIR,
                 timestamp: str | None = None) -> Path:
    snapshot = load_snapshot(snapshot_path)
    retiered = check_retiering(snapshot)
    tunables = load_tunables([o for o in overrides if not o.startswith("world.")])
    cfg = load_world_config([o for o in overrides if o.startswith("world.")])
    run = run_world(snapshot, cfg, tunables, seed, world_name)
    rng = streams(seed)["examples"]
    examples = [share_rows(run, snapshot, j, rng) for j in example_prompt_indices(snapshot, run.world)]
    metrics = clean(compute_metrics(run, snapshot, tunables, seed, world_name, examples))
    config = clean({"scenario": f"world-{world_name}", "world_name": world_name, "seed": seed, "world": cfg,
                    "tunables": {k: tunables[k] for k in ("dive", "tiers", "glicko")},
                    "snapshot": {"file": snapshot_path.name, "sha256": snapshot.header["prompts_sha256"],
                                 "header": snapshot.header, "retiering_checked_prompts": retiered},
                    "engine_version": ENGINE_VERSION})
    stamp = timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = output_dir / run_dir_name(stamp, config["scenario"], config)
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.resolved.json").write_text(json.dumps({**config, "git_commit": git_commit()}, indent=2, sort_keys=True) + "\n")
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    (out / "report.md").write_text(report_markdown(metrics, examples, cfg, tunables, snapshot))
    write_csv(out / "by_percentile.csv", ["percentile", "players", "theta", "valid_rate", "blank_rate", "wrong_rate",
                                          "mean_points_per_prompt", "dive_score", "sampled_dive_median"] + [f"tier_{t}" for t in range(1, 6)],
              [[r["percentile"], r["players"], r["theta"], r["valid_rate"], r["blank_rate"], r["wrong_rate"],
                r["mean_points_per_prompt"], r["dive_score"], r["sampled_dive_median"], *r["tier_mix"]] for r in metrics["by_percentile"]])
    write_csv(out / "pick_shares.csv", ["prompt_id", "answer_rank", "tier", "predicted", "simulated", "sampled"],
              [[e["prompt_id"], rank + 1, int(e["tier"][k]), float(e["predicted"][k]), float(e["simulated"][k]), float(e["sampled"][k])]
               for e in examples for rank, k in enumerate(np.argsort(-e["predicted"], kind="mergesort"))])
    write_csv(out / "prompt_ratings.csv", ["prompt_id", "template", "true_rating"],
              [[int(snapshot.prompt_id[int(j)]), snapshot.template[int(j)], float(run.oracle.prompt_rating[u])]
               for u, j in enumerate(run.world.used)])
    charts(out, metrics, examples, run, snapshot)
    return out
