"""A prompt's starting rating: a linear map from features frozen at approval (rarity values), fitted in the sim."""

from collections.abc import Mapping

from .params import SeedParams


def seed_rating(features: Mapping[str, float], seed: SeedParams, template_offset: float = 0.0) -> float:
    """intercept + sum(coefficient_k * feature_k) + template_offset, summed in sorted coefficient-name order.
    A coefficient without its feature raises KeyError: a prompt frozen without it cannot be seeded."""
    total = seed.intercept
    for name in sorted(seed.coefficients):
        total += seed.coefficients[name] * features[name]
    return total + template_offset


def seed_rd(seed: SeedParams) -> float:
    return seed.rd
