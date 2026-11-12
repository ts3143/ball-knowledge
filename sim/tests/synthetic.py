"""A synthetic snapshot: made-up ids, no names, rated by the pipeline's own model code so it is self-consistent."""

import json
from pathlib import Path

import numpy as np

from rating.params import load_tunables
from simulation.snapshot import canonical, load_model_module


def make_document(n_prompts: int = 12, seed: int = 5) -> dict:
    model = load_model_module()
    mp = model.ModelParams.from_tunables(load_tunables())
    rng = np.random.default_rng(seed)
    prompts = []
    for pid in range(1, n_prompts + 1):
        n = int(rng.integers(15, 40))
        entities = [10_000 + pid * 100 + k for k in range(n)]
        scores = {e: float(rng.normal(7.0, 2.0)) for e in entities}
        weak = frozenset(e for e in entities if rng.random() < 0.1)
        shares = model.softmax_shares(scores)
        model_tiers = model.assign_tiers(scores, shares, mp.cumulative_share)
        tiers = model.effective_tiers(model_tiers, weak, mp)
        gate = model.tier_gate(tiers, shares, mp)
        points = {e: model.answer_points(tiers[e], False, mp) for e in entities}
        expected, mass = model.difficulty_seed(shares, points, scores)
        prompts.append({
            "id": pid, "template": f"template_{pid % 3}", "status": "gate_passed", "competition_id": 900,
            "club_ids": sorted({int(c) for c in rng.choice([11, 12, 13, 14], size=int(rng.integers(0, 3)), replace=False)}),
            "answer_count": n, "rarity": {"model_version": model.MODEL_VERSION, "params": mp.to_params(), "expected_points": expected,
                       "log_recall_mass": mass},
            "top_share": gate["top_share"], "n_eff": gate["n_eff"],
            "answers": [{"entity_id": e, "score": scores[e], "predicted_share": shares[e], "model_tier": model_tiers[e],
                         "tier": tiers[e], "points": model.answer_points(tiers[e], False, mp), "weak": e in weak,
                         "hipster_pick": False} for e in entities]})
    return make_header(prompts)


def make_header(prompts: list[dict]) -> dict:
    import hashlib

    header = {"export_date": "2030-01-01", "git_commit": "synthetic", "model_version": "synthetic",
              "prompt_count": len(prompts), "answer_count": sum(len(p["answers"]) for p in prompts),
              "prompts_sha256": hashlib.sha256(canonical(prompts).encode()).hexdigest()}
    return {"header": header, "prompts": prompts}


def write_snapshot(path: Path, doc: dict) -> Path:
    path.write_text(json.dumps(doc))
    return path
