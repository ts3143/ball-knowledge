import math
import random
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from rarity.features import Features
from rarity.model import (
    MODEL_VERSION,
    ModelParams,
    assign_tiers,
    difficulty_seed,
    effective_tiers,
    hipster_candidates,
    posterior_shares,
    rate_prompt,
    score,
    softmax_shares,
    tier_gate,
)

P = ModelParams(alpha=0.8, beta=0.5, half_life_seasons=10, fame_floor_views=100,
                tier_names=("Bandwagon", "Casual", "Real One", "Deep Cut", "You Know Ball"),
                cumulative_share=(0.50, 0.75, 0.90, 0.98, 1.00), points=(10, 25, 50, 75, 100),
                hipster_pick_points=15, max_bandwagon_answer_share=0.34, weak_answer_max_tier=3)


def feats(fame=1000.0, tie=10.0, since=0.0):
    return Features(0, 0, fame, tie, since)


def table(shares, p=P):
    """Tiers for hand-written shares (the scores are their logs)."""
    scores = {i: math.log(s) for i, s in enumerate(shares)}
    return assign_tiers(scores, dict(enumerate(shares)), p.cumulative_share)


def test_model_version_and_tunables_load():
    assert MODEL_VERSION == "rarity-1"
    tunables = tomllib.loads((Path(__file__).resolve().parents[2] / "tunables.toml").read_text())
    p = ModelParams.from_tunables(tunables)
    assert (p.alpha, p.beta, p.half_life_seasons, p.fame_floor_views) == (0.6, 0.5, 10, 100)
    assert p.max_bandwagon_answer_share == 0.34 and p.cumulative_share[-1] == 1.0
    assert p.points == (10, 25, 50, 75, 100) and p.hipster_pick_points == 15


def test_shares_sum_to_one():
    scores = {i: score(10.0 ** i, i, i, P) for i in range(1, 9)}
    assert math.fsum(softmax_shares(scores).values()) == pytest.approx(1.0, abs=1e-12)


def test_shares_rise_with_each_feature():
    base = score(1000, 10, 5, P)
    assert score(2000, 10, 5, P) > base
    assert score(1000, 20, 5, P) > base
    assert score(1000, 10, 2, P) > base


def test_score_reads_as_the_design_says():
    # ten seasons ago halves the picks
    assert math.exp(score(1000, 10, 10, P) - score(1000, 10, 0, P)) == pytest.approx(0.5)
    # 10x the views is about 6.3x the picks once the floor is small against V
    assert math.exp(score(1e8, 0, 0, P) - score(1e7, 0, 0, P)) == pytest.approx(6.31, abs=0.01)


def test_shares_ignore_a_common_factor_on_views():
    # without the floor, a common factor on every V shifts every score by one constant
    p = replace(P, fame_floor_views=0)
    a = softmax_shares({1: score(1e6, 5, 2, p), 2: score(4e6, 5, 2, p)})
    b = softmax_shares({1: score(1e9, 5, 2, p), 2: score(4e9, 5, 2, p)})
    assert a[1] == pytest.approx(b[1], rel=1e-9)


def test_huge_view_counts_stay_stable():
    shares = softmax_shares({1: score(1e15, 1e6, 0, P), 2: score(0, 0, 40, P)})
    assert shares[1] == pytest.approx(1.0) and 0 <= shares[2] < 1e-12
    assert all(math.isfinite(s) for s in shares.values())


def test_shuffling_the_input_changes_nothing():
    fs = {i: feats(100.0 * i * i, i, i % 7) for i in range(1, 40)}
    first = rate_prompt(fs, P)
    ids = list(fs)
    random.Random(3).shuffle(ids)
    again = rate_prompt({i: fs[i] for i in ids}, P)
    assert first.shares == again.shares and first.tiers == again.tiers and first.scores == again.scores
    assert first.expected_points == again.expected_points


def test_midpoint_tiers_on_a_hand_computed_table():
    # shares .30 .20 .15 .10 .08 .06 .05 .03 .02 .01; midpoints .15 .40 .575 .70 .79 .86 .915 .955 .98 .995
    tiers = table([.30, .20, .15, .10, .08, .06, .05, .03, .02, .01])
    assert [tiers[i] for i in range(10)] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]


def test_a_dominant_answer_is_bandwagon_and_the_rest_spread_below():
    # .70 straddles every cutoff up to .75 but its midpoint .35 puts it in Bandwagon; the .30 left is Casual onwards
    tiers = table([.70, .12, .08, .05, .03, .02])
    assert tiers[0] == 1 and tiers[1] == 3 and tiers[5] == 5
    assert max(tiers.values()) == 5


def test_equal_scores_share_one_tier_even_across_a_cutoff():
    # four equal answers of .25: as a block midpoint .5 is not below .50, so all are Casual;
    # as single answers they would have been split across Bandwagon and Casual
    scores = {i: 0.0 for i in range(4)}
    shares = {i: 0.25 for i in range(4)}
    assert set(assign_tiers(scores, shares, P.cumulative_share).values()) == {2}
    # a block sorts after the higher scores whatever the ids
    scores = {7: 1.0, 1: 0.0, 2: 0.0}
    shares = {7: .6, 1: .2, 2: .2}
    tiers = assign_tiers(scores, shares, P.cumulative_share)
    assert tiers[7] == 1 and tiers[1] == tiers[2] == 3  # block midpoint .6 + .2 = .8


def test_the_top_answer_is_bandwagon_unless_every_score_is_equal():
    assert table([.4, .3, .3])[0] == 1
    assert table([.9, .1])[0] == 1
    # one block holding all the share has midpoint .5, so it is Casual (and the gate fails on the empty tiers)
    assert set(assign_tiers({1: 0.0, 2: 0.0}, {1: .5, 2: .5}, P.cumulative_share).values()) == {2}


def test_a_flat_set_of_twelve_leaves_you_know_ball_empty_and_the_gate_fails():
    shares = {i: 1 / 12 for i in range(12)}
    scores = {i: 0.01 * i for i in range(12)}  # distinct, near-flat
    tiers = assign_tiers(scores, shares, P.cumulative_share)
    gate = tier_gate(tiers, softmax_shares({i: 0.0 for i in range(12)}), P)
    assert gate["tier_counts"][4] == 0 and "empty tier: You Know Ball" in gate["reasons"] and not gate["pass"]


def test_bandwagon_threshold_edge():
    # 50 answers: 17 in Bandwagon is exactly 0.34 of N and passes, 18 fails
    def gate_for(bandwagon):
        tiers = {i: 1 for i in range(bandwagon)} | {i: 2 + (i % 4) for i in range(bandwagon, 50)}
        return tier_gate(tiers, {i: 1 / 50 for i in range(50)}, P)
    assert gate_for(17)["pass"] and gate_for(17)["reasons"] == []
    over = gate_for(18)
    assert not over["pass"] and over["reasons"] == ["Bandwagon too big: 18 of 50 answers (max 17.00)"]


def test_gate_record_has_the_documented_fields_rounded():
    scores = {i: float(-i) for i in range(12)}
    shares = softmax_shares(scores)
    gate = tier_gate(assign_tiers(scores, shares, P.cumulative_share), shares, P)
    assert set(gate) == {"tier_counts", "n", "bandwagon_answers", "max_bandwagon_answer_share", "top_share",
                         "n_eff", "reasons", "pass"}
    assert gate["n"] == 12 and sum(gate["tier_counts"]) == 12
    assert gate["top_share"] == round(gate["top_share"], 6) and gate["n_eff"] == round(gate["n_eff"], 6)


def test_the_hipster_override_replaces_the_tier_points():
    fs = {i: feats(1000.0 * (i + 1) ** 3, 10, 0) for i in range(1, 13)}
    plain, flagged = rate_prompt(fs, P), rate_prompt(fs, P, hipster=frozenset({1}))
    assert plain.points[1] == P.points[plain.tiers[1] - 1] and flagged.points[1] == 15
    assert flagged.tiers == plain.tiers and flagged.points[2] == plain.points[2]


def test_posterior_with_no_picks_is_the_prediction_and_always_sums_to_one():
    predicted = {1: .5, 2: .3, 3: .2}
    assert posterior_shares(predicted, {}, 50) == pytest.approx(predicted)
    post = posterior_shares(predicted, {1: 10, 2: 30, 3: 2}, 50)
    assert math.fsum(post.values()) == pytest.approx(1.0)


def test_fifty_picks_halve_the_priors_weight():
    post = posterior_shares({1: .5, 2: .5}, {2: 50}, 50)
    assert post[1] == pytest.approx(25 / 100) and post[2] == pytest.approx(75 / 100)


def test_posterior_ignores_picks_outside_the_set():
    assert posterior_shares({1: 1.0}, {1: 5, 99: 100}, 50) == pytest.approx({1: 1.0})


def test_hipster_threshold_edges():
    predicted = {1: .02, 2: .98}
    tiers = {1: 4, 2: 1}

    def flagged(picks, tiers=tiers, ratio=3.0):
        return hipster_candidates(predicted, posterior_shares(predicted, picks, 50), picks, tiers, ratio, 5, 3)

    # 5 picks of 100: posterior (1 + 5) / 150 = .04 < 3 * .02 -> not flagged; 8 picks of 100: (1 + 8) / 150 = .06
    assert flagged({1: 5, 2: 95}) == []
    assert flagged({1: 8, 2: 92}) == [1]
    # exactly the ratio: p .02, n = 5 picks -> (1 + 5) / 55 = .109 >= .06 passes; min picks 4 fails the same ratio
    assert flagged({1: 5}) == [1] and flagged({1: 4}) == []
    # the base tier must be Real One or deeper
    assert flagged({1: 8, 2: 92}, tiers={1: 2, 2: 1}) == []
    assert flagged({1: 8, 2: 92}, tiers={1: 3, 2: 1}) == [1]


def test_difficulty_seed():
    shares, points = {1: .5, 2: .3, 3: .2}, {1: 10, 2: 25, 3: 100}
    scores = {1: math.log(5), 2: math.log(3), 3: math.log(2)}
    expected, mass = difficulty_seed(shares, points, scores)
    assert expected == pytest.approx(.5 * 10 + .3 * 25 + .2 * 100)
    assert mass == pytest.approx(math.log(10))
    assert difficulty_seed({}, {}, {}) == (0.0, -math.inf)


def test_rate_prompt_ties_the_pieces_together():
    fs = {i: feats(fame=100.0 * 3 ** i, tie=5.0 * i, since=float(i)) for i in range(1, 16)}
    r = rate_prompt(fs, P)
    assert r.tiers[15] == 1 and min(r.shares, key=r.shares.get) == 1
    assert r.expected_points == pytest.approx(math.fsum(r.shares[e] * r.points[e] for e in fs))
    assert r.gate["n"] == 15


def test_the_weak_cap_applies_to_weak_answers_only_and_the_gate_runs_on_the_capped_tiers():
    assert effective_tiers({1: 1, 2: 3, 3: 4, 4: 5}, frozenset({1, 3, 4}), P) == {1: 1, 2: 3, 3: 3, 4: 3}
    # 15 answers whose deepest tiers hold only weak ones: on model tiers the gate passes, on capped tiers it fails
    fs = {i: feats(fame=1e6 * 0.45 ** i, tie=20.0, since=0.0) for i in range(15)}
    plain = rate_prompt(fs, P)
    assert plain.gate["pass"] and plain.model_gate["pass"]
    deep = frozenset(e for e, t in plain.model_tiers.items() if t >= 4)
    capped = rate_prompt(fs, P, weak=deep)
    assert capped.model_tiers == plain.model_tiers and capped.model_gate == plain.model_gate
    assert all(t == 3 for e, t in capped.tiers.items() if e in deep)
    assert capped.points[max(deep)] == 50 and plain.points[max(deep)] > 50
    assert not capped.gate["pass"] and capped.model_gate["pass"]
    assert {"empty tier: Deep Cut", "empty tier: You Know Ball"} <= set(capped.gate["reasons"])
