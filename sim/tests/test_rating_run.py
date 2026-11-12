import dataclasses
import json

import numpy as np
import pytest

from rating.core import idle_rd
from rating.params import load_tunables
from simulation.commands import fit_seed_command, load_sim_tunables, run_command
from simulation.experiment import Task, aggregate, evaluate_bars, run_task
from simulation.metrics import percentile_matrix
from simulation.oracle import spearman
from simulation.play import idle_rd_array, sample_games
from simulation.runner import sample_dives
from simulation.seedfit import (
    FEATURES,
    SeedData,
    loto_predictions,
    ols,
    propose,
    template_folds,
    tunables_block,
    with_seed,
)
from simulation.snapshot import from_document
from simulation.world import build_world, load_world_config, streams
from tests.synthetic import make_document, write_snapshot

DOC = make_document(n_prompts=24)
SMALL = ["dive.prompts_per_dive=2", "world.theta_sd=4.0", "world.favourites_share=[1.0, 0.0, 0.0]",
         "scenarios.population.players=80", "scenarios.population.dates=12", "scenarios.population.seeds=2",
         "scenarios.testers.players=10", "scenarios.testers.dates=12", "scenarios.testers.seeds=3"]


def small_task(world="model-true", scenario="population", seed=1):
    cfg = load_world_config([o for o in SMALL if o.startswith("world.")])
    cfg.update(players=80 if scenario == "population" else 10, dates=12)
    tun = load_sim_tunables(["dive.prompts_per_dive=2"])
    return Task("unused", world, scenario, seed, cfg, 2, 100.0, (("current", tun),)), tun


def run_small(tmp_path, **kw):
    path = write_snapshot(tmp_path / "prompts-20300101.json", DOC)
    import simulation.experiment as ex

    ex.cached_snapshot.cache_clear()
    task, _ = small_task(**kw)
    return run_task(dataclasses.replace(task, snapshot_path=str(path)))


def test_a_high_signal_world_reaches_estimation_above_point_nine(tmp_path):
    m = run_small(tmp_path)["metrics"]["current"]
    assert m["est.all.012"] > 0.9
    assert m["validity.oracle"] > 0.9
    assert m["est.all.012"] >= m["est.all.003"] - 0.05  # more dives do not hurt


def test_the_idle_rd_array_matches_the_engine_function():
    rd, vol = np.array([50.0, 120.0, 349.0]), np.array([0.06, 0.08, 0.05])
    periods = np.array([0, 3, 400])
    got = idle_rd_array(rd, vol, periods, 350.0)
    assert got == pytest.approx([idle_rd(r, v, int(k), 350.0) for r, v, k in zip(rd, vol, periods, strict=True)], rel=1e-12)


def test_games_are_the_same_draws_as_the_dive_sampler():
    snapshot = from_document(DOC)
    cfg = load_world_config(["world.players=30", "world.dates=5"])
    world = build_world(snapshot, cfg, 2, 3, "model-true")
    g = sample_games(world, snapshot, streams(3)["answers"])
    dives = sample_dives(world, snapshot, streams(3)["answers"])
    summed = np.where(world.plays, 0.0, np.nan)
    np.add.at(summed, (g.player, g.date), g.points)
    assert np.allclose(summed, dives, equal_nan=True)


def test_percentiles_and_baseline_helpers():
    pct = percentile_matrix(np.array([[1.0, np.nan], [3.0, 5.0], [2.0, 4.0]]))
    assert pct[:, 0] == pytest.approx([0, 100, 50])
    assert pct[1, 1] == 100 and pct[2, 1] == 0 and np.isnan(pct[0, 1])
    assert np.isnan(spearman(np.ones(5), np.arange(5.0)))


def test_aggregate_handles_missing_keys_and_nan():
    agg = aggregate([{"a": 1.0, "b": 2.0}, {"a": 3.0}, {"a": float("nan"), "b": 4.0}])
    assert agg["a"]["mean"] == 2.0 and agg["a"]["n"] == 2 and agg["b"]["mean"] == 3.0 and agg["b"]["max"] == 4.0


def test_bars_report_none_for_scenarios_not_run():
    rows = evaluate_bars(None, None)
    assert len(rows) == 9 and all(r["passed"] is None for r in rows)


def test_overrides_can_create_seed_coefficients_but_not_arbitrary_keys():
    t = load_sim_tunables(["glicko.prompt_seed.coefficients.ln_n_eff=12.5", "glicko.prompt_seed.template_offsets.some_template=-4"])
    assert t["glicko"]["prompt_seed"]["coefficients"]["ln_n_eff"] == 12.5
    assert t["glicko"]["prompt_seed"]["template_offsets"] == {"some_template": -4}
    with pytest.raises(KeyError):
        load_sim_tunables(["glicko.nonsense=1"])


# --- seed fit ---------------------------------------------------------------

def synthetic_seed_data(coef=(-60.0, 0.0, 0.0, 0.0), intercept=1800.0, shift=None, seed=2):
    rng = np.random.default_rng(seed)
    templates = [f"t{i % 5}" for i in range(60)]
    features = np.column_stack([rng.normal(12, 1.5, 60), rng.normal(3.5, 0.6, 60), rng.uniform(0.05, 0.4, 60), rng.normal(2.5, 0.5, 60)])
    y = intercept + features @ np.array(coef) + rng.normal(0, 10, 60)
    for t, d in (shift or {}).items():
        y = y + np.where(np.array(templates) == t, d, 0.0)
    return SeedData(np.arange(60), templates, features, y, y + rng.normal(0, 10, 60))


def test_seed_fit_recovers_known_coefficients_and_ships_the_base_feature_alone():
    p = propose(synthetic_seed_data())
    assert p["chosen"] == "log_recall_mass"
    assert p["coefficients"]["log_recall_mass"] == pytest.approx(-60.0, abs=3.0)
    assert p["intercept"] == pytest.approx(1800.0, abs=40.0)
    assert p["template_offsets"] == {}
    assert p["rd"] == 60.0 and p["rd_floor_applied"]  # CV error in the wrong world is about 10, below the floor


def test_seed_fit_adds_a_feature_only_when_it_cuts_cv_error_by_fifteen_percent():
    p = propose(synthetic_seed_data(coef=(-60.0, 0.0, 0.0, 40.0)))
    assert "ln_n_eff" in p["coefficients"]
    assert p["coefficients"]["ln_n_eff"] == pytest.approx(40.0, abs=8.0)
    weak = propose(synthetic_seed_data(coef=(-60.0, 0.0, 0.0, 0.5)))  # a feature that barely matters
    assert weak["chosen"] == "log_recall_mass"


def test_seed_fit_adds_template_offsets_only_above_fifty():
    p = propose(synthetic_seed_data(shift={"t2": 120.0}))
    assert list(p["template_offsets"]) == ["t2"] and abs(p["template_offsets"]["t2"]) > 50
    assert propose(synthetic_seed_data(shift={"t2": 30.0}))["template_offsets"] == {}


def test_seed_rd_is_the_cv_error_in_the_wrong_world_with_a_floor():
    data = synthetic_seed_data()
    wrong = SeedData(data.prompt_ids, data.templates, data.features, data.y_true, data.y_true + np.random.default_rng(1).normal(0, 150, 60))
    p = propose(wrong)
    assert p["cv_rmse_wrong_world"] > 100 and p["rd"] == pytest.approx(p["cv_rmse_wrong_world"]) and not p["rd_floor_applied"]


def test_cross_validation_folds_partition_the_rows_and_never_train_on_the_held_out_template():
    data = synthetic_seed_data(shift={"t1": 500.0})
    folds = template_folds(data.templates)
    assert len(folds) == 5 and (np.sum(folds, axis=0) == 1).all()
    x = np.column_stack([np.ones(60), data.features[:, 0]])
    pred = loto_predictions(x, data.y_true, data.templates)
    held = np.array(data.templates) == "t1"
    manual = x[held] @ ols(x[~held], data.y_true[~held])
    assert pred[held] == pytest.approx(manual)  # the outlier template is predicted without ever seeing itself


def test_the_proposed_block_and_tunables_copy():
    p = propose(synthetic_seed_data(shift={"t2": 120.0}))
    block = tunables_block(p)
    assert block.startswith("[glicko.prompt_seed]") and "template_offsets" in block and "log_recall_mass" in block
    original = load_tunables()["glicko"]["prompt_seed"]["intercept"]
    tun = with_seed(load_tunables(), p)
    assert tun["glicko"]["prompt_seed"]["intercept"] == p["intercept"]
    assert load_tunables()["glicko"]["prompt_seed"]["intercept"] == original  # the file on disk is untouched
    assert set(FEATURES) >= set(p["coefficients"])


# --- determinism ------------------------------------------------------------

def test_run_metrics_are_byte_identical_and_do_not_depend_on_the_number_of_workers(tmp_path):
    path = write_snapshot(tmp_path / "prompts-20300101.json", DOC)
    a = run_command(path, "model-true", "testers", 5, SMALL, tmp_path / "a", workers=1, timestamp="t1")
    b = run_command(path, "model-true", "testers", 5, SMALL, tmp_path / "b", workers=2, timestamp="t2")
    assert (a / "metrics.json").read_bytes() == (b / "metrics.json").read_bytes()
    assert (a / "report.md").read_text() == (b / "report.md").read_text()
    c = run_command(path, "model-true", "testers", 6, SMALL, tmp_path / "c", workers=1, timestamp="t3")
    assert (c / "metrics.json").read_bytes() != (a / "metrics.json").read_bytes()
    assert json.loads((a / "metrics.json").read_text())["runs"]


def test_fit_seed_runs_end_to_end_on_a_small_snapshot(tmp_path):
    path = write_snapshot(tmp_path / "prompts-20300101.json", DOC)
    out, fit = fit_seed_command(path, 1, SMALL + ["scenarios.population.seeds=2"], tmp_path / "o", workers=1, timestamp="t")
    names = {p.name for p in out.iterdir()}
    assert {"report.md", "metrics.json", "pass_bars.csv", "seed_fit.csv", "rank_curve.png", "rd_calibration.png",
            "tester_distribution.png", "seed_fit.png"} <= names
    text = (out / "report.md").read_text()
    assert "## Pass bars" in text and "[glicko.prompt_seed]" in text
    assert fit["rd"] >= 60.0
