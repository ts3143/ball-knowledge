"""Quality gates, as pure functions over the numbers. The size gate runs here; the tier gate is added by the
rarity stage (rarity/model.py) for prompts that pass it. The completeness gate is recorded as not run.
Every gate that ran must pass."""

NOT_RUN = "not_run"

# T's decisions on prompts that passed the gates (python -m prompts review). generate leaves both alone.
APPROVED, REJECTED = "approved", "rejected"
DECIDED_STATUSES = (APPROVED, REJECTED)
# Statuses of prompts that passed every gate: undecided, approved or rejected.
PASSED_STATUSES = ("gate_passed", *DECIDED_STATUSES)


def size_gate(n: int, min_answers: int, max_answers: int) -> dict:
    return {"n": n, "min": min_answers, "max": max_answers, "pass": min_answers <= n <= max_answers}


def gate_results(n: int, quality_gates: dict) -> dict:
    """Size, then tiers (set by the rarity stage when the size gate passes) and completeness: not run."""
    return {
        "size": size_gate(n, quality_gates["min_answers"], quality_gates["max_answers"]),
        "tiers": NOT_RUN,
        "completeness": NOT_RUN,
    }


def status(results: dict) -> str:
    """gate_passed if every gate that ran passed, else gate_failed."""
    ran = [g for g in results.values() if isinstance(g, dict)]
    return "gate_passed" if all(g["pass"] for g in ran) else "gate_failed"


def status_matches(stored: str, results: dict) -> bool:
    """A stored status agrees with the gates: exactly status(results), except that an approved or rejected prompt
    must have passed them (the decision replaced gate_passed)."""
    expected = status(results)
    return stored == expected or (stored in DECIDED_STATUSES and expected == "gate_passed")
