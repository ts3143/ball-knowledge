"""`simulation run` and `simulation fit-seed`: tasks, seed fit, the report."""

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from rating import ENGINE_VERSION
from rating.params import load_tunables, parse_override

from .experiment import Task, aggregate, run_tasks
from .metrics import REPORT_DATES
from .play import FEATURES, prompt_features
from .report_run import write_outputs
from .runner import git_commit
from .seedfit import SeedData, propose, tunables_block, with_seed
from .snapshot import check_retiering, load_snapshot
from .world import WORLDS, load_scenarios, load_section, load_world_config

CREATABLE = ("glicko.prompt_seed.coefficients.", "glicko.prompt_seed.template_offsets.")
FIT_SEEDS = 3  # population seeds the seed map is fitted on


SECTIONS = ("sweep", "seed_fit", "tiers_sensitivity")


def split_overrides(overrides: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(tunables, world, scenarios) overrides; the other config sections' overrides are read with `section_overrides`."""
    world = [o for o in overrides if o.startswith("world.")]
    scen = [o for o in overrides if o.startswith("scenarios.")]
    other = [o for o in overrides if o.startswith(tuple(f"{s}." for s in SECTIONS))]
    rest = [o for o in overrides if o not in world and o not in scen and o not in other]
    return rest, world, scen


def section_overrides(overrides: list[str], name: str) -> list[str]:
    return [o for o in overrides if o.startswith(f"{name}.")]


def load_sim_tunables(overrides: list[str]) -> dict:
    """tunables.toml with overrides. Coefficients and template offsets may be created (a feature the file does not use
    yet); every other override must name an existing key."""
    creating = [o for o in overrides if o.startswith(CREATABLE)]
    tun = load_tunables([o for o in overrides if o not in creating])
    for o in creating:
        parts, value = parse_override(o)
        node = tun
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return tun


def scenario_cfg(cfg: dict, scenarios: dict, name: str) -> dict:
    return {**cfg, "players": scenarios[name]["players"], "dates": scenarios[name]["dates"]}


def make_tasks(snapshot_path: Path, world: str, scenario: str, base_seed: int, cfg: dict, scenarios: dict, tunables: dict,
               configs: tuple) -> list[Task]:
    sc = scenario_cfg(cfg, scenarios, scenario)
    ppd, scale = int(tunables["dive"]["prompts_per_dive"]), float(max(tunables["tiers"]["points"]))
    return [Task(str(snapshot_path), world, scenario, base_seed + k, sc, ppd, scale, configs)
            for k in range(int(scenarios[scenario]["seeds"]))]


def collect(runs: list[dict], labels: list[str]) -> dict:
    out = {}
    for label in labels:
        ms = [r["metrics"][label] for r in runs]
        date = max((d for d in REPORT_DATES if f"e2e.all.{d:03d}" in ms[0]), default=None)  # 28 for the testers
        out[label] = {"agg": aggregate(ms), "seeds": [r["seed"] for r in runs], "date": date,
                      "per_seed": {k: [m[f"{k}.all.{date:03d}"] for m in ms] for k in ("e2e", "est")} if date else {}}
    return out


def seed_data(snapshot, truth: dict) -> SeedData:
    """Rows = prompts played in the fit seeds; true ratings averaged over those seeds, per world."""
    by_world = {}
    for world, runs in truth.items():
        acc: dict[int, list[float]] = {}
        for r in runs:
            for pid, y in zip(r["prompt_ids"], r["true_rating"], strict=True):
                acc.setdefault(pid, []).append(y)
        by_world[world] = {pid: float(np.mean(v)) for pid, v in acc.items()}
    pids = sorted(set(by_world["model-true"]) & set(by_world["model-wrong"]))
    index = {int(p): j for j, p in enumerate(snapshot.prompt_id)}
    feats = np.array([[prompt_features(snapshot, index[p])[f] for f in FEATURES] for p in pids])
    return SeedData(np.array(pids), [snapshot.template[index[p]] for p in pids], feats,
                    np.array([by_world["model-true"][p] for p in pids]), np.array([by_world["model-wrong"][p] for p in pids]))


def meta_for(snapshot, title: str) -> dict:
    return {"title": title, "snapshot_sha256": snapshot.header["prompts_sha256"], "git_commit": git_commit()}


def config_doc(scenario: str, base_seed: int, cfg: dict, scenarios: dict, labelled: dict, snapshot, snapshot_path: Path) -> dict:
    return {"scenario": scenario, "seed": base_seed, "world": cfg, "scenarios": scenarios,
            "tunables": {label: {k: t[k] for k in ("dive", "tiers", "glicko")} for label, t in labelled.items()},
            "snapshot": {"file": snapshot_path.name, "sha256": snapshot.header["prompts_sha256"], "header": snapshot.header},
            "engine_version": ENGINE_VERSION}


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def run_command(snapshot_path: Path, world: str, scenario: str, seed: int, overrides: list[str], out_dir: Path,
                workers: int, timestamp: str | None = None) -> Path:
    snapshot = load_snapshot(snapshot_path)
    check_retiering(snapshot)
    tun_over, world_over, scen_over = split_overrides(overrides)
    tunables, cfg, scenarios = load_sim_tunables(tun_over), load_world_config(world_over), load_scenarios(scen_over)
    started = time.time()
    runs = run_tasks(make_tasks(snapshot_path, world, scenario, seed, cfg, scenarios, tunables, (("current", tunables),)), workers)
    results = {(world, scenario): collect(runs, ["current"])}
    title = f"{world}, {scenario} scenario, seed {seed}"
    out = write_outputs(out_dir, f"run-{scenario}-{world}", config_doc(f"run-{scenario}-{world}", seed, cfg, scenarios, {"current": tunables},
                                                                     snapshot, snapshot_path),
                        results, None, None, meta_for(snapshot, title), timestamp or stamp())
    print(f"{len(runs)} seeds in {time.time() - started:.1f}s")
    return out


def fit_seed_command(snapshot_path: Path, seed: int, overrides: list[str], out_dir: Path, workers: int,
                     timestamp: str | None = None) -> tuple[Path, dict]:
    snapshot = load_snapshot(snapshot_path)
    check_retiering(snapshot)
    tun_over, world_over, scen_over = split_overrides(overrides)
    tunables, cfg, scenarios = load_sim_tunables(tun_over), load_world_config(world_over), load_scenarios(scen_over)
    started = time.time()
    n_fit = min(FIT_SEEDS, int(scenarios["population"]["seeds"]))
    fit_scen = {**scenarios, "population": {**scenarios["population"], "seeds": n_fit}}
    truth = {w: run_tasks(make_tasks(snapshot_path, w, "population", seed, cfg, fit_scen, tunables, ()), workers) for w in WORLDS}
    print(f"truth for the seed fit: {time.time() - started:.1f}s")
    data = seed_data(snapshot, truth)
    fit = propose(data, int(load_section("seed_fit", section_overrides(overrides, "seed_fit"))["min_template_prompts"]))
    proposed = with_seed(tunables, fit)
    configs = (("current", tunables), ("proposed", proposed))
    results = {}
    for w in WORLDS:
        for sc in ("population", "testers"):
            t0 = time.time()
            runs = run_tasks(make_tasks(snapshot_path, w, sc, seed, cfg, scenarios, tunables, configs), workers)
            results[(w, sc)] = collect(runs, ["current", "proposed"])
            print(f"{w} {sc}: {len(runs)} seeds, {time.time() - started:.1f}s total ({time.time() - t0:.1f}s)")
    doc = config_doc("fit-seed", seed, cfg, scenarios, dict(configs), snapshot, snapshot_path)
    out = write_outputs(out_dir, "fit-seed", doc, results, fit, data, meta_for(snapshot, f"seed fit and both scenarios, seed {seed}"),
                        timestamp or stamp())
    print(tunables_block(fit))
    return out, fit


def default_workers() -> int:
    return os.cpu_count() or 1
