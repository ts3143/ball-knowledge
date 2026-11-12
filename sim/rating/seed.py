"""A prompt's starting rating: a linear map from features frozen at approval (rarity values), fitted in the sim."""

import math
from collections.abc import Mapping

from .params import SeedParams
from .validate import finite


def prompt_features(rarity: Mapping, gate_results: Mapping) -> dict[str, float]:
    """The seed features of a prompts row, from its `rarity` and `gate_results` jsonb: log_recall_mass is
    rarity.log_recall_mass and ln_n_eff is ln(gate_results.tiers.n_eff). A missing, non-numeric or non-finite value,
    or an n_eff that is not above 0, raises ValueError: a prompt without them cannot be seeded."""
    try:
        recall = rarity["log_recall_mass"]
        n_eff = gate_results["tiers"]["n_eff"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"prompt is missing {e} (needs rarity.log_recall_mass and gate_results.tiers.n_eff)") from e
    finite(recall, "rarity.log_recall_mass")
    finite(n_eff, "gate_results.tiers.n_eff")
    if n_eff <= 0:
        raise ValueError(f"gate_results.tiers.n_eff must be above 0, got {n_eff}")
    return {"log_recall_mass": float(recall), "ln_n_eff": math.log(n_eff)}


def seed_rating(features: Mapping[str, float], seed: SeedParams, template_offset: float = 0.0) -> float:
    """intercept + sum(coefficient_k * feature_k) + template_offset, summed in sorted coefficient-name order.
    A coefficient without its feature raises KeyError, a non-finite feature or offset ValueError: a prompt frozen
    without them cannot be seeded."""
    finite(template_offset, "template_offset")
    total = seed.intercept
    for name in sorted(seed.coefficients):
        total += seed.coefficients[name] * finite(features[name], f"feature {name}")
    return total + template_offset


def seed_rd(seed: SeedParams) -> float:
    return seed.rd
