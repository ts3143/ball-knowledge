"""Quality gates, as pure functions over the numbers. Step 2 chunk 2 has the size gate only;
the mix and completeness gates are recorded as not run."""

NOT_RUN = "not_run"


def size_gate(n: int, min_answers: int, max_answers: int) -> dict:
    return {"n": n, "min": min_answers, "max": max_answers, "pass": min_answers <= n <= max_answers}


def gate_results(n: int, quality_gates: dict) -> dict:
    return {
        "size": size_gate(n, quality_gates["min_answers"], quality_gates["max_answers"]),
        "mix": NOT_RUN,
        "completeness": NOT_RUN,
    }


def status(results: dict) -> str:
    """gate_passed if every gate that ran passed, else gate_failed."""
    ran = [g for g in results.values() if isinstance(g, dict)]
    return "gate_passed" if all(g["pass"] for g in ran) else "gate_failed"
