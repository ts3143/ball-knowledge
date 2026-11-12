from datetime import date

import pytest

from rating.display import club_visible, is_uncertain, rd_now
from rating.golden import golden_params
from rating.params import Params, load_params, load_tunables, parse_override
from rating.seed import seed_rating, seed_rd
from rating.types import State


def test_the_loader_reads_the_glicko_tunables():
    # Tuning values change; this checks that every key loads into the right field, not what the values are.
    raw = load_tunables()["glicko"]
    p = load_params()
    assert (p.tau, p.volatility_epsilon, p.child_scope_initial_rd, p.game_weight) == (
        raw["tau"], raw["volatility_epsilon"], raw["child_scope_initial_rd"], raw["game_weight"])
    seed = raw["prompt_seed"]
    assert (p.seed.intercept, p.seed.rd, p.seed.coefficients) == (seed["intercept"], seed["rd"], seed["coefficients"])
    assert p.points_scale == 100.0


def test_set_overrides_replace_existing_keys_and_unknown_keys_fail():
    t = load_tunables(["glicko.tau=0.8", "glicko.prompt_seed.coefficients.log_recall_mass=12.5"])
    assert t["glicko"]["tau"] == 0.8
    assert Params.from_tunables(t).seed.coefficients["log_recall_mass"] == 12.5
    with pytest.raises(KeyError):
        load_tunables(["glicko.tua=0.8"])
    with pytest.raises(KeyError):
        load_tunables(["nosuch.tau=1"])
    with pytest.raises(ValueError):
        parse_override("glicko.tau")


def test_params_round_trip_through_a_dict():
    p = golden_params()
    assert Params.from_dict(p.to_dict()) == p


def test_the_seed_is_a_linear_map_with_an_optional_offset():
    p = golden_params()  # coefficient 40 on log_recall_mass
    assert seed_rating({"log_recall_mass": -2.0}, p.seed) == 1500.0 - 80.0
    assert seed_rating({"log_recall_mass": -2.0}, p.seed, template_offset=15.0) == 1500.0 - 65.0
    zero = Params.from_dict({**p.to_dict(), "seed": {**p.to_dict()["seed"], "coefficients": {"log_recall_mass": 0.0}}})
    assert seed_rating({"log_recall_mass": -2.0}, zero.seed) == 1500.0  # zero coefficients: the intercept
    assert seed_rd(p.seed) == 350.0
    with pytest.raises(KeyError):
        seed_rating({}, p.seed)


def test_rd_now_grows_with_idle_dates_and_the_marker_follows_the_threshold():
    p = golden_params()
    st = State(1500.0, 50.0, 0.06, 40, date(2030, 1, 1))
    assert rd_now(st, date(2030, 1, 2), p) == 50.0  # the next date: no growth yet
    assert rd_now(st, date(2030, 1, 3), p) > 50.0
    assert rd_now(State(1500.0, 50.0, 0.06, 40, date(2029, 1, 1)), date(2030, 1, 1), p) == pytest.approx(205, abs=1)
    assert not is_uncertain(st, date(2030, 1, 2), p)
    assert is_uncertain(st, date(2030, 6, 1), p)
    assert is_uncertain(None, date(2030, 1, 1), p)  # never played: RD is the initial one


def test_club_visibility_starts_at_the_minimum_prompts():
    p = golden_params()
    mk = lambda n: State(1500.0, 100.0, 0.06, n, date(2030, 1, 1))
    assert not club_visible(mk(19), p) and club_visible(mk(20), p) and not club_visible(None, p)
