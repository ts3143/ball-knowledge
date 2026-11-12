"""Quality gates, as pure functions over the numbers. The completeness gate is recorded as not run.
The mix gate is a provisional pageview proxy and advisory in step 2 (T): it records what it would
decide but never changes a prompt's status. The tier gate (rarity model, step 3) checks the real tier spread
and blocks."""

import math

NOT_RUN = "not_run"


def size_gate(n: int, min_answers: int, max_answers: int) -> dict:
    return {"n": n, "min": min_answers, "max": max_answers, "pass": min_answers <= n <= max_answers}


def quantile(values: list[float], q: float) -> float:
    """The q-quantile by linear interpolation between order statistics."""
    if not values:
        return math.inf
    s = sorted(values)
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def fame_cutoffs(fames: list[int], quality_gates: dict) -> tuple[float, float]:
    """(obvious, obscure) fame cutoffs over the fames of one entity type."""
    return (quantile(fames, quality_gates["mix_obvious_quantile"]),
            quantile(fames, quality_gates["mix_obscure_quantile"]))


def mix_gate(fames: list[int], cutoffs: tuple[float, float], quality_gates: dict) -> dict:
    """fames: total pageviews of each answer, 0 for none. Obvious: at or above the obvious cutoff.
    Obscure: below the obscure cutoff, or no pageviews at all. "pass" is what the gate would decide;
    the cutoffs are those of the answers' entity type, so a stored result can be explained."""
    obvious_cut, obscure_cut = cutoffs
    obvious = sum(f > 0 and f >= obvious_cut for f in fames)
    obscure = sum(f == 0 or f < obscure_cut for f in fames)
    mn_obvious, mn_obscure = quality_gates["mix_min_obvious"], quality_gates["mix_min_obscure"]
    return {"obvious": obvious, "obscure": obscure, "min_obvious": mn_obvious, "min_obscure": mn_obscure,
            "obvious_cutoff": obvious_cut, "obscure_cutoff": obscure_cut, "advisory": True,
            "pass": obvious >= mn_obvious and obscure >= mn_obscure}


def gate_results(n: int, quality_gates: dict, mix: dict | None = None) -> dict:
    """The tier gate is added by the rarity stage, for prompts that pass the size gate."""
    return {
        "size": size_gate(n, quality_gates["min_answers"], quality_gates["max_answers"]),
        "mix": mix if mix is not None else NOT_RUN,
        "tiers": NOT_RUN,
        "completeness": NOT_RUN,
    }


def status(results: dict) -> str:
    """gate_passed if every blocking gate that ran passed, else gate_failed. Advisory gates are ignored."""
    ran = [g for g in results.values() if isinstance(g, dict) and not g.get("advisory")]
    return "gate_passed" if all(g["pass"] for g in ran) else "gate_failed"
