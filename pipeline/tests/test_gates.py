from prompts.gates import gate_results, size_gate, status

QUALITY_GATES = {"min_answers": 15, "max_answers": 300}


def test_size_gate_boundaries():
    assert size_gate(14, 15, 300)["pass"] is False
    assert size_gate(15, 15, 300)["pass"] is True
    assert size_gate(300, 15, 300)["pass"] is True
    assert size_gate(301, 15, 300)["pass"] is False


def test_size_gate_records_its_numbers():
    assert size_gate(20, 15, 300) == {"n": 20, "min": 15, "max": 300, "pass": True}


def test_gate_results_hold_size_tiers_and_completeness_and_no_mix():
    results = gate_results(20, QUALITY_GATES)
    assert list(results) == ["size", "tiers", "completeness"]
    assert results["tiers"] == "not_run" and results["completeness"] == "not_run"


def test_status_passes_when_the_gates_that_ran_pass():
    assert status(gate_results(20, QUALITY_GATES)) == "gate_passed"


def test_status_fails_when_a_gate_that_ran_fails():
    assert status(gate_results(14, QUALITY_GATES)) == "gate_failed"
    assert status(gate_results(301, QUALITY_GATES)) == "gate_failed"
    results = {**gate_results(20, QUALITY_GATES), "tiers": {"pass": False}}
    assert status(results) == "gate_failed"
    assert status({**results, "tiers": {"pass": True}}) == "gate_passed"
