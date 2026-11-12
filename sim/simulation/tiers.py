"""Tier variants: re-tier the snapshot's prompts with different cumulative cutoffs and points, using the pipeline's own
model code (pipeline/rarity/model.py), so a variant is exactly what the pipeline would have stored."""

import dataclasses
from dataclasses import dataclass

import numpy as np

from .snapshot import Snapshot, load_model_module


@dataclass(frozen=True)
class TierVariant:
    name: str
    cumulative_share: tuple[float, ...] | None = None  # None = as stored
    points: tuple[int, ...] | None = None


def variant_from_config(name: str, spec: dict) -> TierVariant:
    return TierVariant(name, tuple(spec["cumulative_share"]) if "cumulative_share" in spec else None,
                       tuple(spec["points"]) if "points" in spec else None)


def retier(snapshot: Snapshot, variant: TierVariant) -> tuple[Snapshot, int]:
    """The snapshot with tier and points replaced by the variant's, and the number of prompts that fail the tier gate under it
    (on the effective tiers, the weak cap as stored). Scores, shares and flags do not change."""
    model = load_model_module()
    tier, points = snapshot.tier.copy(), snapshot.points.copy()
    failing = 0
    for j, p in enumerate(snapshot.prompts):
        mp = model.ModelParams.from_params(p["rarity"]["params"])
        changes = {}
        if variant.cumulative_share is not None:
            changes["cumulative_share"] = variant.cumulative_share
        if variant.points is not None:
            changes["points"] = variant.points
        mp = dataclasses.replace(mp, **changes)
        scores = {a["entity_id"]: a["score"] for a in p["answers"]}
        weak = frozenset(a["entity_id"] for a in p["answers"] if a["weak"])
        hipster = frozenset(a["entity_id"] for a in p["answers"] if a["hipster_pick"])
        shares = model.softmax_shares(scores)
        tiers = model.effective_tiers(model.assign_tiers(scores, shares, mp.cumulative_share), weak, mp)
        sl = snapshot.answers(j)
        for k, a in zip(range(sl.start, sl.stop), p["answers"], strict=True):
            e = a["entity_id"]
            tier[k] = tiers[e]
            points[k] = model.answer_points(tiers[e], e in hipster, mp)
        failing += 0 if model.tier_gate(tiers, shares, mp)["pass"] else 1
    return dataclasses.replace(snapshot, tier=tier, points=points.astype(np.float64)), failing


def tunables_for(tunables: dict, variant: TierVariant) -> dict:
    import copy

    out = copy.deepcopy(tunables)
    if variant.cumulative_share is not None:
        out["tiers"]["cumulative_share"] = list(variant.cumulative_share)
    if variant.points is not None:
        out["tiers"]["points"] = list(variant.points)
    return out
