import math
import random

import pytest

from rating.core import MAX_ITERATIONS, SCALE, Opponent, VolatilityError, idle_rd, new_volatility, rate_period
from rating.golden import GLICKMAN, golden_params

P = golden_params()
EQUAL = Opponent(1500.0, 200.0, 0.5)


def test_glickman_example_matches_the_paper():
    i, pub, tol = GLICKMAN["inputs"], GLICKMAN["published"], GLICKMAN["published_abs_tolerance"]
    out = rate_period(i["rating"], i["rd"], i["volatility"], 0, [Opponent(*o) for o in i["opponents"]], P)
    assert out.rating == pytest.approx(pub["rating"], abs=tol["rating"])
    assert out.rd == pytest.approx(pub["rd"], abs=tol["rd"])
    assert out.volatility == pytest.approx(pub["volatility"], abs=tol["volatility"])


def test_no_games_grows_phi_exactly_and_keeps_rating_and_volatility():
    out = rate_period(1600.0, 80.0, 0.07, 4, [], P)
    phi = 80.0 / SCALE
    assert out.rd == pytest.approx(SCALE * math.sqrt(phi * phi + 4 * 0.07 * 0.07), rel=1e-12)
    assert (out.rating, out.volatility, out.iterations) == (1600.0, 0.07, 0)


def test_idle_growth_is_capped_at_initial_rd():
    assert idle_rd(340.0, 0.06, 5000, 350.0) == pytest.approx(350.0)
    assert idle_rd(50.0, 0.06, 0, 350.0) == 50.0  # no idle periods: untouched
    assert idle_rd(50.0, 0.06, 364, 350.0) == pytest.approx(205, abs=1)  # a year away, the design's example


def test_a_score_equal_to_expected_keeps_the_rating_and_shrinks_rd():
    out = rate_period(1500.0, 200.0, 0.06, 0, [EQUAL], P)
    assert out.rating == pytest.approx(1500.0, abs=1e-9)
    assert out.rd < 200.0


def test_a_score_of_a_quarter_against_an_equal_opponent_lowers_the_rating():
    assert rate_period(1500.0, 200.0, 0.06, 0, [Opponent(1500.0, 200.0, 0.25)], P).rating < 1500.0


def test_rating_rises_with_the_score_and_with_the_opponents_rating():
    by_score = [rate_period(1500.0, 150.0, 0.06, 0, [Opponent(1500.0, 150.0, s)], P).rating
                for s in (0.0, 0.15, 0.25, 0.5, 0.75, 1.0)]
    assert by_score == sorted(by_score) and len(set(by_score)) == len(by_score)
    by_opp = [rate_period(1500.0, 150.0, 0.06, 0, [Opponent(r, 150.0, 0.5)], P).rating
              for r in (1200.0, 1400.0, 1500.0, 1700.0, 1900.0)]
    assert by_opp == sorted(by_opp) and len(set(by_opp)) == len(by_opp)


@pytest.mark.parametrize("rd,gap,score", [(1.0, 3000.0, 0.0), (1.0, -3000.0, 1.0), (350.0, 3000.0, 1.0),
                                          (350.0, -3000.0, 0.0), (1.0, 3000.0, 1.0), (350.0, 0.0, 0.5)])
def test_extremes_stay_finite(rd, gap, score):
    out = rate_period(1500.0, rd, 0.06, 0, [Opponent(1500.0 + gap, rd, score)], P)
    assert all(math.isfinite(x) for x in (out.rating, out.rd, out.volatility))
    assert 0.0 < out.rd <= 350.0


def test_game_weight_one_is_standard_and_other_weights_change_the_result():
    i = GLICKMAN["inputs"]
    opps = [Opponent(*o) for o in i["opponents"]]
    standard = rate_period(i["rating"], i["rd"], i["volatility"], 0, opps, golden_params(game_weight=1.0))
    heavier = rate_period(i["rating"], i["rd"], i["volatility"], 0, opps, golden_params(game_weight=3.0))
    assert standard.rating == pytest.approx(1464.05067, abs=1e-4)  # the paper's value
    assert heavier.rd < standard.rd  # more weight on each game: more certainty


def test_phi_star_is_capped_at_initial_rd():
    capped = rate_period(1500.0, 350.0, 0.5, 0, [Opponent(1500.0, 350.0, 0.5)], golden_params(initial_rd=350.0))
    assert capped.rd <= 350.0
    # without the cap (a huge limit) the same period would leave RD above 350 for this volatility
    loose = rate_period(1500.0, 350.0, 0.5, 0, [Opponent(1500.0, 350.0, 0.5)], golden_params(initial_rd=1e6))
    assert loose.rd > capped.rd


def test_random_cases_converge_within_the_limit_and_use_both_brackets():
    rng = random.Random(7)
    iterations, brackets = [], set()
    for _ in range(10_000):
        opps = [Opponent(rng.uniform(900, 2300), rng.uniform(30, 350), rng.choice([0, 0.1, 0.15, 0.25, 0.5, 1.0]))
                for _ in range(rng.randint(1, 10))]
        params = golden_params(tau=rng.choice([0.2, 0.3, 0.5, 0.8, 1.2]), game_weight=rng.choice([1.0, 2.0, 3.0]))
        out = rate_period(rng.uniform(1000, 2000), rng.uniform(30, 350), rng.uniform(0.02, 0.12), 0, opps, params)
        iterations.append(out.iterations)
        brackets.add(out.bracket)
    assert max(iterations) <= MAX_ITERATIONS
    assert brackets == {"log", "scan"}


def test_more_than_the_iteration_limit_raises(monkeypatch):
    monkeypatch.setattr("rating.core.MAX_ITERATIONS", 1)
    with pytest.raises(VolatilityError):
        new_volatility(1.0, 0.06, 0.1, 0.5, 0.5, 1e-12)


def test_summation_order_is_the_callers_order():
    # Float addition is not associative: the same games in another order may differ in the last digits, which is
    # why the engine fixes the order. Each order is itself repeatable.
    opps = [Opponent(1400.0 + 37 * k, 50.0 + 11 * k, 0.15 * (k % 5)) for k in range(9)]
    a = rate_period(1500.0, 120.0, 0.06, 0, opps, P)
    assert a == rate_period(1500.0, 120.0, 0.06, 0, list(opps), P)
