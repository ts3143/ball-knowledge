"""`simulation sweep` and `simulation tiers`: the grid, the winner's detail tables, the tier variants, the recommendations."""

import copy
import time
from pathlib import Path

import numpy as np

from .commands import (
    collect,
    load_sim_tunables,
    make_tasks,
    scenario_cfg,
    section_overrides,
    seed_data,
    split_overrides,
    stamp,
)
from .experiment import Task, aggregate, evaluate_bars, run_tasks
from .metrics import CLUB_STEPS
from .play import FEATURES  # noqa: F401
from .report_sweep import write_sweep_outputs, write_tiers_outputs
from .seedfit import propose, with_seed
from .snapshot import check_retiering, load_snapshot
from .sweep import WORLD_KEYS, combos, entries, get, label, pick, with_glicko
from .tiers import retier, tunables_for, variant_from_config
from .world import WORLDS, load_scenarios, load_section, load_world_config

FIT_SEEDS = 3


def settings(overrides: list[str]):
    tun_o, world_o, scen_o = split_overrides(overrides)
    return (load_sim_tunables(tun_o), load_world_config(world_o), load_scenarios(scen_o),
            load_section("sweep", section_overrides(overrides, "sweep")),
            load_section("seed_fit", section_overrides(overrides, "seed_fit")),
            load_section("tiers_sensitivity", section_overrides(overrides, "tiers_sensitivity")))


def fit_for(snapshot_path, snapshot, tunables, cfg, scenarios, seed, workers, min_template_prompts, tier_variant=None):
    """Oracle truth in both worlds (population seeds) -> the proposed seed map and the tunables carrying it."""
    n_fit = min(FIT_SEEDS, int(scenarios["population"]["seeds"]))
    fit_scen = {**scenarios, "population": {**scenarios["population"], "seeds": n_fit}}
    truth = {}
    for w in WORLDS:
        tasks = [Task(**{**t.__dict__, "tier_variant": tier_variant}) for t in make_tasks(snapshot_path, w, "population", seed, cfg, fit_scen, tunables, ())]
        truth[w] = run_tasks(tasks, workers)
    data = seed_data(snapshot, truth)
    fit = propose(data, min_template_prompts)
    return fit, with_seed(tunables, fit), data


def analysis_of(sweep: dict) -> dict:
    return {k: list(sweep[k]) for k in ("uncertain_rd_thresholds", "uncertain_after_dives", "club_min_prompts", "lock_games")}


def run_grid(snapshot_path, base, cfg, scenarios, sweep, seed, workers) -> dict[str, dict[str, dict]]:
    """aggregate[world][label] over the population seeds, for every combination."""
    grid = combos(sweep)
    per = int(sweep["configs_per_task"])
    chunks = [grid[i:i + per] for i in range(0, len(grid), per)]
    sc = scenario_cfg(cfg, scenarios, "population")
    ppd, scale = int(base["dive"]["prompts_per_dive"]), float(max(base["tiers"]["points"]))
    tasks, keys = [], []
    for w in WORLDS:
        for k in range(int(scenarios["population"]["seeds"])):
            for chunk in chunks:
                configs = tuple((label(c), with_glicko(base, c)) for c in chunk)
                tasks.append(Task(str(snapshot_path), w, "population", seed + k, sc, ppd, scale, configs, None, analysis_of(sweep)))
                keys.append(w)
    runs = run_tasks(tasks, workers)
    acc: dict[tuple[str, str], list[dict]] = {}
    for w, r in zip(keys, runs, strict=True):
        for lab, m in r["metrics"].items():
            acc.setdefault((w, lab), []).append(m)
    return {w: {lab: aggregate(ms) for (ww, lab), ms in acc.items() if ww == w} for w in WORLDS}


def run_testers(snapshot_path, base, cfg, scenarios, seed, workers, configs, tier_variant=None) -> dict[str, dict]:
    out = {}
    for w in WORLDS:
        tasks = [Task(**{**t.__dict__, "tier_variant": tier_variant}) for t in make_tasks(snapshot_path, w, "testers", seed, cfg, scenarios, base, configs)]
        out[w] = collect(run_tasks(tasks, workers), [lab for lab, _ in configs])
    return out


# ---------------------------------------------------------------------------
# What T decides besides the grid: the uncertain marker and club visibility
# ---------------------------------------------------------------------------

def uncertain_rows(aggs: dict[str, dict], sweep: dict) -> list[dict]:
    rows = []
    for t in sweep["uncertain_rd_thresholds"]:
        row = {"threshold": t}
        f1s = []
        for w, agg in aggs.items():
            share, mu, ms = get(agg, f"unc.t{t:03d}.share"), get(agg, f"unc.t{t:03d}.mis_uncertain"), get(agg, f"unc.t{t:03d}.mis_settled")
            overall = share * mu + (1 - share) * ms
            recall = share * mu / overall if overall > 0 else float("nan")
            f1s.append(2 * mu * recall / (mu + recall) if mu + recall > 0 else float("nan"))
            row[w] = {"share": share, "mis_uncertain": mu, "mis_settled": ms, "ratio": mu / ms if ms > 0 else float("nan"),
                      "recall": recall, **{f"after_{k}": get(agg, f"unc.k{k:02d}.t{t:03d}.share") for k in sweep["uncertain_after_dives"]}}
        valid = [x for x in f1s if not np.isnan(x)]
        row["f1"] = float(np.mean(valid)) if valid else float("nan")
        rows.append(row)
    return rows


def recommend_uncertain(rows: list[dict], current: int) -> tuple[int, str]:
    """The threshold that best separates misplaced from settled players: highest F1 of 'is marked uncertain' as a detector of
    'misplaced by more than 10 percentile points' (precision = misplaced rate among the uncertain, recall = share of all
    misplaced observations that are marked). Ties keep the current value."""
    valid = [r for r in rows if not np.isnan(r["f1"])]
    if not valid:
        return current, "no threshold marks any observation, so the current value is kept"
    best = max(valid, key=lambda r: (round(r["f1"], 3), r["threshold"] == current))
    return int(best["threshold"]), f"highest F1 as a misplacement detector ({best['f1']:.3f})"


def club_rows(aggs: dict[str, dict], sweep: dict) -> list[dict]:
    rows = []
    for n in sweep["club_min_prompts"]:
        row = {"min_prompts": n}
        for w, agg in aggs.items():
            row[w] = {"median_date": get(agg, f"clubs.v{n:02d}.median"), "share": get(agg, f"clubs.v{n:02d}.share"),
                      "spearman": get(agg, f"clubs.n{n}.spearman") if n in CLUB_STEPS else float("nan"),
                      "delta": get(agg, f"clubs.n{n}.delta_spearman") if n in CLUB_STEPS else float("nan"),
                      "rd": get(agg, f"clubs.n{n}.rd") if n in CLUB_STEPS else float("nan"),
                      "clubs": get(agg, f"clubs.n{n}.clubs") if n in CLUB_STEPS else float("nan")}
        rows.append(row)
    return rows


def recommend_club(rows: list[dict], current: int, dates: int) -> tuple[int, str]:
    """The largest minimum that still shows the rating to most daily fans in good time and ranks them well: at least 90% of daily
    fans reach it, the median date is within half the season, and the within-club Spearman is at least 0.85 (the estimation bar),
    in both worlds. The smallest candidate if none qualifies."""
    ok = [r for r in rows if all(r[w]["spearman"] >= 0.85 and r[w]["share"] >= 0.9 and r[w]["median_date"] <= dates / 2 for w in WORLD_KEYS)]
    if ok:
        best = max(ok, key=lambda r: r["min_prompts"])
        return int(best["min_prompts"]), "the largest minimum that at least 90% of daily fans reach, with a median date within half the season and a within-club Spearman of at least 0.85"
    best = min(rows, key=lambda r: r["min_prompts"])
    return int(best["min_prompts"]), "no candidate reaches 90% of daily fans within half the season with Spearman 0.85; the smallest is shown"


def lock_rows(aggs: dict[str, dict], sweep: dict) -> list[dict]:
    return [{"games": n, **{w: {"rmse": get(agg, f"lock.n{n:03d}.rmse"), "bias": get(agg, f"lock.n{n:03d}.bias"),
                                "prompts": get(agg, f"lock.n{n:03d}.prompts")} for w, agg in aggs.items()}}
            for n in sweep["lock_games"]]


# ---------------------------------------------------------------------------
# Tier sensitivity
# ---------------------------------------------------------------------------

def tier_rows(snapshot_path: Path, snapshot, base: dict, cfg: dict, scenarios: dict, seed: int, workers: int, tiers_cfg: dict,
              min_template_prompts: int, sweep: dict) -> list[dict]:
    rows = []
    for name, spec in tiers_cfg["variants"].items():
        v = variant_from_config(name, spec)
        t0 = time.time()
        _snap_v, failing = retier(snapshot, v)
        base_v = tunables_for(base, v)
        fit, tun_v, _ = fit_for(snapshot_path, snapshot, base_v, cfg, scenarios, seed, workers, min_template_prompts, v)
        configs = (("variant", tun_v),)
        pop = [Task(**{**t.__dict__, "tier_variant": v, "analysis": analysis_of(sweep)})
               for t in make_tasks(snapshot_path, "model-true", "population", seed, cfg, scenarios, tun_v, configs)]
        pop_agg = collect(run_tasks(pop, workers), ["variant"])["variant"]["agg"]
        test = collect(run_tasks([Task(**{**t.__dict__, "tier_variant": v}) for t in make_tasks(snapshot_path, "model-true", "testers", seed, cfg, scenarios, tun_v, configs)], workers), ["variant"])["variant"]
        tagg = test["agg"]
        rows.append({
            "variant": name, "points": list(tun_v["tiers"]["points"]), "cumulative_share": list(tun_v["tiers"]["cumulative_share"]),
            "validity": get(pop_agg, "validity.oracle"), "e2e_28_population": get(pop_agg, "e2e.all.028"),
            "e2e_28_testers_median": get(tagg, "e2e.all.028", "median"), "e2e_28_testers_p10": get(tagg, "e2e.all.028", "p10"),
            "e2e_28_testers_p90": get(tagg, "e2e.all.028", "p90"), "dive_median": get(pop_agg, "see.dive_median"),
            "dive_p10": get(pop_agg, "see.dive_p10"), "dive_p90": get(pop_agg, "see.dive_p90"),
            "gate_failing": failing, "prompts": len(snapshot.prompts), "ykb_points_share": get(pop_agg, "tiers.ykb_points_share"),
            "seed_cv_rmse_true": fit["base_cv_rmse_true"], "seconds": time.time() - t0})
        print(f"tier variant {name}: {time.time() - t0:.1f}s")
    return rows


def tier_decision(rows: list[dict], min_gain: float) -> dict:
    current = next(r for r in rows if r["variant"] == "current")
    best = max((r for r in rows if r["variant"] != "current"), key=lambda r: r["validity"], default=None)
    gain = best["validity"] - current["validity"] if best else 0.0
    return {"min_gain": min_gain, "best_variant": best["variant"] if best else None, "best_gain": gain, "change": bool(best and gain >= min_gain)}


# ---------------------------------------------------------------------------
# The recommendation
# ---------------------------------------------------------------------------

def toml_value(v) -> str:
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(x)}" for k, x in v.items()) + " }"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, float):
        return f"{v:.6g}" if v != int(v) or abs(v) < 1e6 else str(v)
    return str(v)


def tunables_diff(current: dict, recommended: dict) -> list[str]:
    """Unified-diff style lines for every recommended key that differs from the current tunables."""
    lines = []
    for section in ("glicko", "glicko.prompt_seed", "tiers"):
        cur, rec = current, recommended
        for part in section.split("."):
            cur, rec = cur[part], rec[part]
        changed = []
        for k in rec:
            if isinstance(rec[k], dict) and section == "glicko":
                continue  # sub-tables have their own section
            if k not in cur or cur[k] != rec[k]:
                if k in cur and isinstance(rec[k], (int, float)) and isinstance(cur[k], (int, float)) and float(cur[k]) == float(rec[k]):
                    continue
                changed.append(k)
        if changed:
            lines.append(f" [{section}]")
            for k in changed:
                if k in cur:
                    lines.append(f"-{k} = {toml_value(cur[k])}")
                lines.append(f"+{k} = {toml_value(rec[k])}")
    return lines or [" (no change)"]


def sweep_command(snapshot_path: Path, seed: int, overrides: list[str], out_dir: Path, workers: int, timestamp: str | None = None):
    snapshot = load_snapshot(snapshot_path)
    check_retiering(snapshot)
    tunables, cfg, scenarios, sweep, seed_fit, tiers_cfg = settings(overrides)
    started = time.time()
    fit, base, data = fit_for(snapshot_path, snapshot, tunables, cfg, scenarios, seed, workers, int(seed_fit["min_template_prompts"]))
    print(f"seed fit: {time.time() - started:.1f}s; chosen {fit['chosen']}, offsets {fit['template_offsets']}")
    per_world = run_grid(snapshot_path, base, cfg, scenarios, sweep, seed, workers)
    print(f"grid ({len(combos(sweep))} combinations x 2 worlds x {scenarios['population']['seeds']} seeds): {time.time() - started:.1f}s")
    ranked = entries(per_world, sweep)
    chosen = pick(ranked, sweep, 2)
    winner, runner = chosen[0], chosen[1] if len(chosen) > 1 else chosen[0]
    win_tun, run_tun = with_glicko(base, winner.combo), with_glicko(base, runner.combo)
    testers = run_testers(snapshot_path, base, cfg, scenarios, seed, workers,
                          ((label(winner.combo), win_tun), (label(runner.combo), run_tun)) if runner is not winner else ((label(winner.combo), win_tun),))
    print(f"testers: {time.time() - started:.1f}s")

    win_aggs = {w: per_world[w][label(winner.combo)] for w in WORLDS}
    unc = uncertain_rows(win_aggs, sweep)
    unc_rec, unc_why = recommend_uncertain(unc, int(tunables["glicko"]["uncertain_rd_threshold"]))
    clubs = club_rows(win_aggs, sweep)
    club_rec, club_why = recommend_club(clubs, int(tunables["glicko"]["club_rating_min_prompts"]), int(scenarios["population"]["dates"]))
    locks = lock_rows(win_aggs, sweep)

    recommended = copy.deepcopy(win_tun)
    recommended["glicko"]["uncertain_rd_threshold"] = unc_rec
    recommended["glicko"]["club_rating_min_prompts"] = club_rec
    trows = tier_rows(snapshot_path, snapshot, recommended, cfg, scenarios, seed, workers, tiers_cfg, int(seed_fit["min_template_prompts"]), sweep)
    decision = tier_decision(trows, float(tiers_cfg["min_validity_gain"]))
    if decision["change"]:
        v = next(r for r in trows if r["variant"] == decision["best_variant"])
        recommended["tiers"]["points"], recommended["tiers"]["cumulative_share"] = v["points"], v["cumulative_share"]
    print(f"tiers: {time.time() - started:.1f}s")

    result = {"fit": fit, "seed_data": data, "ranked": ranked, "winner": winner, "runner_up": runner, "per_world": per_world,
              "testers": testers, "uncertain": unc, "uncertain_rec": (unc_rec, unc_why), "clubs": clubs, "club_rec": (club_rec, club_why),
              "locks": locks, "tier_rows": trows, "tier_decision": decision, "current": tunables, "recommended": recommended,
              "sweep": sweep, "scenarios": scenarios, "cfg": cfg, "snapshot": snapshot, "snapshot_path": snapshot_path, "seed": seed,
              "bars": {}}
    for tag, entry in (("recommended", winner), ("runner-up", runner)):
        for w in WORLDS:
            agg = per_world[w][label(entry.combo)]
            if unc_rec != int(tunables["glicko"]["uncertain_rd_threshold"]):  # the bar is read at the recommended threshold
                agg = dict(agg)
                mu, ms = get(agg, f"unc.t{unc_rec:03d}.mis_uncertain"), get(agg, f"unc.t{unc_rec:03d}.mis_settled")
                agg["rd.misplaced_ratio"] = {"mean": mu / ms if ms else float("nan")}
            result["bars"][(w, tag)] = evaluate_bars(agg, testers[w][label(entry.combo)]["agg"])
    out = write_sweep_outputs(out_dir, result, timestamp or stamp())
    print(f"total {time.time() - started:.1f}s")
    return out, result


def tiers_command(snapshot_path: Path, seed: int, overrides: list[str], out_dir: Path, workers: int, timestamp: str | None = None):
    snapshot = load_snapshot(snapshot_path)
    check_retiering(snapshot)
    tunables, cfg, scenarios, sweep, seed_fit, tiers_cfg = settings(overrides)
    started = time.time()
    rows = tier_rows(snapshot_path, snapshot, tunables, cfg, scenarios, seed, workers, tiers_cfg, int(seed_fit["min_template_prompts"]), sweep)
    decision = tier_decision(rows, float(tiers_cfg["min_validity_gain"]))
    out = write_tiers_outputs(out_dir, rows, decision, snapshot, tunables, timestamp or stamp())
    print(f"tiers: {time.time() - started:.1f}s")
    return out, rows, decision
