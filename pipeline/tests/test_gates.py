from prompts.gates import fame_cutoffs, gate_results, mix_gate, quantile, size_gate, status

QUALITY_GATES = {"min_answers": 15, "max_answers": 300, "mix_obvious_quantile": 0.75, "mix_obscure_quantile": 0.5,
                 "mix_min_obvious": 3, "mix_min_obscure": 3}


def test_size_gate_boundaries():
    assert size_gate(14, 15, 300)["pass"] is False
    assert size_gate(15, 15, 300)["pass"] is True
    assert size_gate(300, 15, 300)["pass"] is True
    assert size_gate(301, 15, 300)["pass"] is False


def test_size_gate_records_its_numbers():
    assert size_gate(20, 15, 300) == {"n": 20, "min": 15, "max": 300, "pass": True}


def test_gate_results_marks_other_gates_not_run():
    results = gate_results(20, QUALITY_GATES)
    assert results["mix"] == "not_run"
    assert results["completeness"] == "not_run"


def test_status_passes_when_the_gates_that_ran_pass():
    assert status(gate_results(20, QUALITY_GATES)) == "gate_passed"


def test_status_fails_when_a_gate_that_ran_fails():
    assert status(gate_results(14, QUALITY_GATES)) == "gate_failed"
    assert status(gate_results(301, QUALITY_GATES)) == "gate_failed"


def test_quantile_interpolates_between_order_statistics():
    assert quantile([40, 0, 10, 20, 30], 0.5) == 20
    assert quantile([0, 10], 0.75) == 7.5
    assert quantile([5], 0.75) == 5
    assert quantile([0, 10, 20, 30, 40], 1.0) == 40


def test_fame_cutoffs_use_the_tunable_quantiles():
    assert fame_cutoffs([0, 10, 20, 30, 40], QUALITY_GATES) == (30, 20)


CUTOFFS = (100, 50)


def test_mix_gate_boundaries_are_at_or_above_obvious_and_below_obscure():
    # 100 is obvious (at the cutoff); 50 is not obscure (at the cutoff); 49 is obscure.
    mix = mix_gate([100, 100, 100, 50, 49, 49, 49], CUTOFFS, QUALITY_GATES)
    assert mix == {"obvious": 3, "obscure": 3, "min_obvious": 3, "min_obscure": 3, "pass": True}
    assert mix_gate([100, 100, 99, 49, 49, 49], CUTOFFS, QUALITY_GATES)["pass"] is False  # 2 obvious
    assert mix_gate([100, 100, 100, 50, 50, 49], CUTOFFS, QUALITY_GATES)["pass"] is False  # only 1 obscure


def test_mix_gate_no_pageviews_counts_as_obscure_even_with_a_zero_cutoff():
    mix = mix_gate([0, 0, 0, 7, 7, 7], (0, 0), QUALITY_GATES)
    assert (mix["obvious"], mix["obscure"]) == (3, 3)  # zeros are obscure, never obvious
    assert mix_gate([0, 0, 0], CUTOFFS, QUALITY_GATES)["obscure"] == 3


def test_status_combines_size_and_mix():
    good = mix_gate([100] * 3 + [0] * 3, CUTOFFS, QUALITY_GATES)
    bad = mix_gate([0] * 6, CUTOFFS, QUALITY_GATES)
    assert status(gate_results(20, QUALITY_GATES, good)) == "gate_passed"
    assert status(gate_results(20, QUALITY_GATES, bad)) == "gate_failed"  # size fine, mix fails
    assert status(gate_results(14, QUALITY_GATES, good)) == "gate_failed"  # mix fine, size fails


def test_status_ignores_gates_not_run_but_not_failed_ones():
    assert status({"size": {"pass": True}, "mix": {"pass": False}, "completeness": "not_run"}) == "gate_failed"
