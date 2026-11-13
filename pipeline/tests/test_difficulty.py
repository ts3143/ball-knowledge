"""Seed difficulty (the sim's seed code imported by path) and the bands. No database."""

import math
import tomllib

from ingest.config import REPO_ROOT
from prompts.difficulty import seed_difficulty, tercile_bands

TUNABLES = tomllib.loads((REPO_ROOT / "tunables.toml").read_text())
SEED = TUNABLES["glicko"]["prompt_seed"]


def test_the_seed_is_the_tunables_linear_map_of_the_frozen_features():
    mass, n_eff = -4.25, 12.5
    expected = (SEED["intercept"] + SEED["coefficients"]["log_recall_mass"] * mass
                + SEED["coefficients"]["ln_n_eff"] * math.log(n_eff))
    got = seed_difficulty(TUNABLES, {"log_recall_mass": mass}, {"tiers": {"n_eff": n_eff}})
    assert math.isclose(got, expected, rel_tol=1e-12)


def test_a_prompt_without_its_features_has_no_seed():
    gates = {"tiers": {"n_eff": 10.0}}
    assert seed_difficulty(TUNABLES, None, gates) is None
    assert seed_difficulty(TUNABLES, {}, gates) is None
    assert seed_difficulty(TUNABLES, {"log_recall_mass": -3.0}, {"tiers": "not_run"}) is None
    assert seed_difficulty(TUNABLES, {"log_recall_mass": -3.0}, {"tiers": {"n_eff": 0}}) is None


def test_terciles_split_the_pool_by_seed_then_id():
    seeds = {i: 1600.0 + i for i in range(1, 10)}
    bands = tercile_bands(seeds)
    assert [bands[i] for i in range(1, 10)] == ["easy"] * 3 + ["medium"] * 3 + ["hard"] * 3
    assert tercile_bands({1: 5.0, 2: 5.0, 3: 5.0}) == {1: "easy", 2: "medium", 3: "hard"}  # ties break by id
    assert tercile_bands({7: 1.0}) == {7: "easy"} and tercile_bands({}) == {}
