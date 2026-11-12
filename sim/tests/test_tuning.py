import dataclasses
import json

import numpy as np
import pytest

from simulation.commands import load_sim_tunables
from simulation.seedfit import SeedData, propose
from simulation.snapshot import from_document
from simulation.sweep import Entry, combos, entries, label, pick, simplicity
from simulation.tiers import TierVariant, retier, tunables_for
from simulation.tuning import (
    club_rows,
    recommend_club,
    recommend_uncertain,
    sweep_command,
    tiers_command,
    tunables_diff,
    uncertain_rows,
)
from simulation.world import load_section
from tests.synthetic import make_document, write_snapshot

SWEEP = load_section("sweep")
DOC = make_document(n_prompts=24)
SMALL = ["dive.prompts_per_dive=2", "world.theta_sd=4.0", "world.favourites_share=[1.0, 0.0, 0.0]",
         "scenarios.population.players=60", "scenarios.population.dates=12", "scenarios.population.seeds=2",
         "scenarios.testers.players=10", "scenarios.testers.dates=12", "scenarios.testers.seeds=3",
         "sweep.tau=[0.5, 0.8]", "sweep.child_scope_initial_rd=[250]", "sweep.game_weight=[1.0, 2.0]", "sweep.configs_per_task=2",
         "sweep.coverage_min=0.0", "sweep.lock_games=[5, 20]", "sweep.club_min_prompts=[2, 4]", "sweep.uncertain_after_dives=[1, 3]",
         "sweep.uncertain_rd_thresholds=[80, 110]"]


# --- the min_template_prompts rule -------------------------------------------

def seed_data_with_shifted_templates():
    rng = np.random.default_rng(5)
    templates = ["big"] * 30 + ["mid"] * 12 + ["tiny"] * 1 + ["rest"] * 17
    n = len(templates)
    features = np.column_stack([rng.normal(12, 1.5, n), rng.normal(3.5, 0.6, n), rng.uniform(0.05, 0.4, n), rng.normal(2.5, 0.5, n)])
    shift = np.array([{"mid": 150.0, "tiny": 200.0}.get(t, 0.0) for t in templates])
    y = 1800.0 - 60.0 * features[:, 0] + shift + rng.normal(0, 5, n)
    return SeedData(np.arange(n), templates, features, y, y + rng.normal(0, 5, n))


def test_a_template_offset_needs_enough_prompts():
    data = seed_data_with_shifted_templates()
    p = propose(data, min_template_prompts=10)
    assert "mid" in p["template_offsets"] and abs(p["template_offsets"]["mid"]) > 50
    assert "tiny" not in p["template_offsets"]  # one prompt, however far off
    assert p["template_prompts"]["tiny"] == 1 and abs(p["template_mean_residuals"]["tiny"]) > 50
    assert "mid" not in propose(data, min_template_prompts=13)["template_offsets"]
    assert "tiny" in propose(data, min_template_prompts=1)["template_offsets"]


# --- choosing the winner -----------------------------------------------------

def fake_agg(objective, cov2, ratio=9.0):
    agg = {f"est.all.{d:03d}": {"mean": objective} for d in range(7, 62)}
    agg.update({"rd.cov2.all": {"mean": cov2}, "rd.cov1.all": {"mean": 0.7}, "rd.misplaced_ratio": {"mean": ratio},
                "placement.median": {"mean": 2.0}, "inflation.per30": {"mean": 0.0}})
    return agg


def fake_grid(table):
    """table[combo] = {world: (objective, cov2)}"""
    return {w: {label(c): fake_agg(*v[w]) for c, v in table.items()} for w in ("model-true", "model-wrong")}


def tiny_cfg():
    return {**SWEEP, "tau": [0.5, 0.8], "child_scope_initial_rd": [250], "game_weight": [1.0, 2.0, 3.0]}


def test_the_winner_is_the_best_combination_feasible_in_both_worlds():
    cfg = tiny_cfg()
    table = {
        (0.5, 250, 1.0): {"model-true": (0.990, 0.995), "model-wrong": (0.990, 0.995)},   # best score but outside the band
        (0.5, 250, 2.0): {"model-true": (0.985, 0.95), "model-wrong": (0.986, 0.99)},     # feasible in one world only
        (0.5, 250, 3.0): {"model-true": (0.980, 0.94), "model-wrong": (0.981, 0.93)},     # feasible in both
        (0.8, 250, 1.0): {"model-true": (0.970, 0.95), "model-wrong": (0.970, 0.95)},     # feasible, worse
        (0.8, 250, 2.0): {"model-true": (0.975, 0.50), "model-wrong": (0.975, 0.95)},
        (0.8, 250, 3.0): {"model-true": (0.960, 0.95), "model-wrong": (0.960, 0.95)},
    }
    ranked = entries(fake_grid(table), cfg)
    assert [e.feasible for e in ranked].count(True) == 3
    first, second = pick(ranked, cfg, 2)
    assert first.combo == (0.5, 250, 3.0) and second.combo == (0.8, 250, 1.0)
    assert first.feasible and first.score == pytest.approx(0.9805)


def test_ties_go_to_the_more_standard_values():
    cfg = tiny_cfg()
    same = {"model-true": (0.9800, 0.95), "model-wrong": (0.9800, 0.95)}
    table = {(0.5, 250, 2.0): same, (0.8, 250, 1.0): {w: (0.9799, 0.95) for w in same}, (0.5, 250, 3.0): {w: (0.9803, 0.95) for w in same},
             (0.8, 250, 2.0): {w: (0.9700, 0.95) for w in same}, (0.5, 250, 1.0): {w: (0.9700, 0.5) for w in same},
             (0.8, 250, 3.0): {w: (0.9700, 0.95) for w in same}}
    best = pick(entries(fake_grid(table), cfg), cfg, 1)[0]
    assert best.combo == (0.8, 250, 1.0)  # within 0.0005 of the best, and game weight 1 is the most standard
    assert simplicity((0.5, 250, 1.0), cfg) < simplicity((0.5, 250, 2.0), cfg) < simplicity((0.5, 250, 3.0), cfg)
    assert simplicity((0.5, 250, 2.0), cfg) < simplicity((0.2, 250, 2.0), cfg)  # tau outside Glickman's range loses


def test_without_a_feasible_combination_the_nearest_to_the_band_is_reported():
    cfg = tiny_cfg()
    table = {c: {w: (0.98, 0.99 + 0.001 * k) for w in ("model-true", "model-wrong")} for k, c in enumerate(combos(cfg))}
    ranked = entries(fake_grid(table), cfg)
    assert not any(e.feasible for e in ranked)
    assert pick(ranked, cfg, 1)[0].combo == combos(cfg)[0]
    assert isinstance(ranked[0], Entry)


def test_the_diff_lists_only_changes_and_the_marker_rules_are_data_driven():
    cur = load_sim_tunables([])
    rec = load_sim_tunables(["glicko.tau=0.8", "glicko.game_weight=2", "glicko.prompt_seed.coefficients.ln_n_eff=3"])
    rec["glicko"]["game_weight"] = 2.0
    diff = tunables_diff(cur, rec)
    assert "-tau = 0.5" in diff and "+tau = 0.8" in diff and "+game_weight = 2" in diff and not any("initial_rd" in x for x in diff)
    assert tunables_diff(cur, cur) == [" (no change)"]
    rows = [{"threshold": t, "f1": f} for t, f in ((80, 0.2), (110, 0.3), (150, 0.1))]
    assert recommend_uncertain(rows, 80)[0] == 110
    assert recommend_uncertain([{"threshold": 80, "f1": float("nan")}], 110)[0] == 110
    clubs = [{"min_prompts": n, **{w: {"median_date": d, "share": s, "spearman": sp} for w in ("model-true", "model-wrong")}}
             for n, d, s, sp in ((5, 10, 1.0, 0.86), (10, 25, 0.97, 0.9), (15, 41, 0.78, 0.93))]
    assert recommend_club(clubs, 20, 61)[0] == 10  # 15 is reached by too few fans
    assert club_rows and uncertain_rows


# --- tiers -------------------------------------------------------------------

def test_the_current_variant_reproduces_the_stored_tiers_and_points():
    snapshot = from_document(DOC)
    same, failing = retier(snapshot, TierVariant("current"))
    assert (same.tier == snapshot.tier).all() and (same.points == snapshot.points).all() and isinstance(failing, int)
    flat, _ = retier(snapshot, TierVariant("flat", points=(10, 20, 40, 70, 100)))
    assert (flat.tier == snapshot.tier).all() and (flat.points != snapshot.points).any()
    cut, _ = retier(snapshot, TierVariant("cut", cumulative_share=(0.3, 0.5, 0.7, 0.9, 1.0)))
    assert (cut.tier != snapshot.tier).any()
    assert tunables_for(load_sim_tunables([]), TierVariant("x", points=(1, 2, 3, 4, 5)))["tiers"]["points"] == [1, 2, 3, 4, 5]


# --- the commands end to end --------------------------------------------------

def test_the_sweep_is_deterministic_whatever_the_worker_count(tmp_path):
    path = write_snapshot(tmp_path / "prompts-20300101.json", DOC)
    a, res = sweep_command(path, 1, SMALL, tmp_path / "a", workers=1, timestamp="t1")
    b, _ = sweep_command(path, 1, SMALL, tmp_path / "b", workers=2, timestamp="t2")
    assert (a / "metrics.json").read_bytes() == (b / "metrics.json").read_bytes()
    assert (a / "report.md").read_text() == (b / "report.md").read_text()
    assert res["winner"].combo in combos({**SWEEP, "tau": [0.5, 0.8], "child_scope_initial_rd": [250], "game_weight": [1.0, 2.0]})
    metrics = json.loads((a / "metrics.json").read_text())
    assert len(metrics["grid"]) == 4 and metrics["winner"] == list(res["winner"].combo)
    names = {p.name for p in a.iterdir()}
    assert {"report.md", "grid.csv", "grid_feasibility.png", "marker_and_clubs.png", "tier_sensitivity.png", "tester_distribution.png"} <= names
    assert "```diff" in (a / "report.md").read_text()


def test_the_tiers_command_writes_a_table_with_the_current_variant(tmp_path):
    path = write_snapshot(tmp_path / "prompts-20300101.json", DOC)
    out, rows, decision = tiers_command(path, 1, SMALL, tmp_path / "o", workers=1, timestamp="t")
    assert next(r["variant"] for r in rows) == "current" and isinstance(rows[0]["gate_failing"], int)
    assert dataclasses.is_dataclass(TierVariant("x")) and "change" in decision
    assert (out / "tiers.csv").exists() and "Tier sensitivity" in (out / "report.md").read_text()


def test_a_diverged_combination_is_never_feasible_even_with_a_perfect_score():
    cfg = tiny_cfg()
    table = {c: {w: (0.97, 0.95) for w in ("model-true", "model-wrong")} for c in combos(cfg)}
    grid = fake_grid(table)
    bad = (0.5, 250, 3.0)
    grid["model-true"][label(bad)] = {**fake_agg(0.999, 0.95), "engine.diverged": {"mean": 1.0}}
    ranked = entries(grid, cfg)
    entry = next(e for e in ranked if e.combo == bad)
    assert entry.diverged and not entry.feasible
    assert pick(ranked, cfg, 1)[0].combo != bad
    nan_agg = {"engine.diverged": {"mean": 0.0}}
    grid["model-wrong"][label((0.8, 250, 3.0))] = nan_agg  # undefined metrics are infeasible too
    assert not next(e for e in entries(grid, cfg) if e.combo == (0.8, 250, 3.0)).feasible
