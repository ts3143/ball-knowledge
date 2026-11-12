import copy
import json

import numpy as np
import pytest

from rating.core import SCALE
from simulation.answers import (
    BLANK,
    VALID,
    WRONG,
    expected_points,
    outcome_probs,
    sample_prompt,
    sigmoid,
    solve_c0,
    valid_rate_of_median_player,
)
from simulation.oracle import fit_oracle, solve_true_bk, spearman
from simulation.runner import world_report
from simulation.snapshot import SnapshotError, check_retiering, from_document
from simulation.world import build_world, load_world_config, streams
from tests.synthetic import make_document, make_header, write_snapshot

DOC = make_document()
FAST = ["world.players=40", "world.dates=4", "dive.prompts_per_dive=2"]


def test_closed_form_matches_a_monte_carlo_average():
    rng = np.random.default_rng(1)
    scores = rng.normal(7.0, 2.0, 25)
    points = rng.choice([10.0, 25.0, 50.0, 75.0, 100.0], 25)
    n = 200_000
    theta = np.full(n, 0.8)
    lam = np.full(n, 0.3)
    wrong = np.full(n, 0.08)
    o = outcome_probs(theta, lam, wrong, scores, c0=8.0)
    drawn = sample_prompt(o, points, rng)
    for code, p in ((VALID, o.p_valid[0]), (BLANK, o.p_blank[0]), (WRONG, o.p_wrong[0])):
        assert (drawn.outcome == code).mean() == pytest.approx(p, abs=4 * np.sqrt(p * (1 - p) / n))
    assert o.p_valid[0] + o.p_blank[0] + o.p_wrong[0] == pytest.approx(1.0)
    expected = expected_points(o, points)[0]
    assert drawn.points.mean() == pytest.approx(expected, abs=4 * drawn.points.std() / np.sqrt(n))
    picks = np.bincount(drawn.answer[drawn.outcome == VALID], minlength=25) / (drawn.outcome == VALID).sum()
    assert np.abs(picks - o.q[0]).max() < 0.01


def test_strategy_runs_from_predicted_shares_to_the_rarest_known_answer():
    scores = np.array([9.0, 6.0, 3.0, 0.0])
    everyone_knows = {"theta_eff": np.array([50.0]), "wrong": np.array([0.0]), "scores": scores, "c0": 0.0}
    q0 = outcome_probs(lam=np.array([0.0]), **everyone_knows).q[0]
    q1 = outcome_probs(lam=np.array([1.0]), **everyone_knows).q[0]
    shares = np.exp(scores) / np.exp(scores).sum()
    assert q0 == pytest.approx(shares)  # lambda 0: exactly the predicted shares
    assert q1.argmax() == 3 and q1[3] > 0.95  # lambda 1: the rarest


def test_c0_hits_the_target_valid_rate():
    snapshot = from_document(DOC)
    scores = [snapshot.score[snapshot.answers(j)] for j in range(snapshot.n_prompts)]
    c0 = solve_c0(scores, 0.70, 0.0875)
    assert valid_rate_of_median_player(c0, scores, 0.0875) == pytest.approx(0.70, abs=1e-8)
    with pytest.raises(ValueError):
        solve_c0(scores, 0.99999, 0.5)  # unreachable with half the answers wrong


def test_the_oracle_recovers_known_ratings_from_noise_free_data():
    rng = np.random.default_rng(3)
    u_true, v_true = rng.normal(0, 1.2, 60), rng.normal(0.5, 0.8, 40)
    fit = fit_oracle(sigmoid(u_true[:, None] - v_true[None, :]))
    shift = np.median(u_true)
    assert fit.u == pytest.approx(u_true - shift, abs=1e-5)
    assert fit.v == pytest.approx(v_true - shift, abs=1e-5)
    assert fit.rmse < 1e-6
    assert fit.player_rating[np.argsort(u_true)[30]] == pytest.approx(1500.0, abs=SCALE * 0.1)
    assert spearman(fit.u, u_true) == pytest.approx(1.0)


def test_the_oracle_pins_the_median_player_and_true_bk_inverts_the_mean_result():
    rng = np.random.default_rng(4)
    u_true, v_true = rng.normal(0, 1.0, 51), rng.normal(0, 0.5, 30)
    fit = fit_oracle(sigmoid(u_true[:, None] - v_true[None, :]))
    assert np.median(fit.player_rating) == pytest.approx(1500.0)
    target = sigmoid(u_true[:, None] - v_true[None, :]).mean(axis=1)
    assert solve_true_bk(fit.v, target) == pytest.approx(fit.u, abs=1e-5)


def test_spearman_handles_ties_and_order():
    assert spearman(np.array([1, 2, 3, 4.0]), np.array([10, 20, 30, 40.0])) == pytest.approx(1.0)
    assert spearman(np.array([1, 2, 3, 4.0]), np.array([4, 3, 2, 1.0])) == pytest.approx(-1.0)
    assert -1 <= spearman(np.array([1, 1, 2, 3.0]), np.array([1, 2, 2, 3.0])) <= 1


def test_spearman_ties_are_averaged_on_the_unsorted_input():
    assert spearman(np.array([1, 3, 2, 1.0]), np.array([0, 4, 3, 2.0])) == pytest.approx(0.9486832980505139, abs=1e-15)


def test_retiering_passes_on_a_consistent_snapshot_and_fails_on_a_tampered_one():
    assert check_retiering(from_document(DOC)) == len(DOC["prompts"])
    tampered = copy.deepcopy(DOC)
    tampered["prompts"][2]["answers"][0]["tier"] = 1 + tampered["prompts"][2]["answers"][0]["tier"] % 5
    with pytest.raises(SnapshotError, match="re-tiering"):
        check_retiering(from_document(make_header(tampered["prompts"])))  # hash is valid; tiers are not
    with pytest.raises(SnapshotError, match="sha256"):
        from_document(tampered)  # stale hash in the header


def test_world_population_does_not_depend_on_the_world_kind():
    snapshot = from_document(DOC)
    cfg = load_world_config(["world.players=30", "world.dates=4"])
    a = build_world(snapshot, cfg, 2, 7, "model-true")
    b = build_world(snapshot, cfg, 2, 7, "model-wrong")
    assert (a.theta == b.theta).all() and (a.plays == b.plays).all() and (a.used == b.used).all()
    assert not np.allclose(a.prompt_scores[0], b.prompt_scores[0])
    assert a.plays.sum(axis=1).min() >= 0 and a.plays.shape == (30, 4)
    assert streams(1)["answers"].random() != streams(2)["answers"].random()


def test_a_small_run_is_deterministic_and_writes_every_output(tmp_path):
    path = write_snapshot(tmp_path / "prompts-20300101.json", DOC)
    one = world_report(path, "model-true", 3, FAST, tmp_path / "a", "t1")
    two = world_report(path, "model-true", 3, FAST, tmp_path / "b", "t2")
    assert (one / "metrics.json").read_bytes() == (two / "metrics.json").read_bytes()
    names = {p.name for p in one.iterdir()}
    assert {"config.resolved.json", "metrics.json", "report.md", "by_percentile.csv", "pick_shares.csv", "prompt_ratings.csv",
            "dive_score_by_percentile.png", "pick_shares.png", "prompt_ratings_by_template.png"} <= names
    metrics = json.loads((one / "metrics.json").read_text())
    assert metrics["players"] == 40 and -1 <= metrics["validity_spearman_oracle_vs_theta"] <= 1
    other = world_report(path, "model-true", 4, FAST, tmp_path / "c", "t3")
    assert (other / "metrics.json").read_bytes() != (one / "metrics.json").read_bytes()


def test_a_tampered_snapshot_aborts_the_run(tmp_path):
    bad = copy.deepcopy(DOC)
    bad["prompts"][0]["answers"][0]["points"] += 1
    path = write_snapshot(tmp_path / "prompts-20300101.json", make_header(bad["prompts"]))
    with pytest.raises(SnapshotError):
        world_report(path, "model-true", 1, FAST, tmp_path / "out", "t")
