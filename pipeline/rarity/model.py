"""The pick-share model: score, share, tier, points and the tier gate, as pure functions over the features.
Nothing here touches the database. Definitions are in the step 3 design (~/.claude/plans/step3-rarity-design.md,
sections 2 to 4 and 7). Bump MODEL_VERSION whenever a feature definition or the formula changes.
"""

import math
from dataclasses import dataclass

MODEL_VERSION = "rarity-1"
WEAK_FLAG = "weak_stint"  # prompt_answers.flags value of an answer whose dates are uncertain
_ROUND = 6
_EPS = 1e-9


@dataclass(frozen=True)
class ModelParams:
    alpha: float
    beta: float
    half_life_seasons: float
    fame_floor_views: float
    tier_names: tuple[str, ...]
    cumulative_share: tuple[float, ...]
    points: tuple[int, ...]
    hipster_pick_points: int
    max_bandwagon_answer_share: float
    weak_answer_max_tier: int  # an answer flagged weak_stint never counts deeper than this tier

    def to_params(self) -> dict:
        """The parameters as stored in prompts.rarity, enough to recompute a stored prompt."""
        return {
            "alpha": self.alpha, "beta": self.beta, "half_life_seasons": self.half_life_seasons,
            "fame_floor_views": self.fame_floor_views, "tier_names": list(self.tier_names),
            "cumulative_share": list(self.cumulative_share), "points": list(self.points),
            "hipster_pick_points": self.hipster_pick_points,
            "max_bandwagon_answer_share": self.max_bandwagon_answer_share,
            "weak_answer_max_tier": self.weak_answer_max_tier}

    @classmethod
    def from_params(cls, params: dict) -> "ModelParams":
        """The inverse of to_params; other keys in `params` (fame weights, feature tunables) are ignored."""
        return cls(
            alpha=params["alpha"], beta=params["beta"], half_life_seasons=params["half_life_seasons"],
            fame_floor_views=params["fame_floor_views"], tier_names=tuple(params["tier_names"]),
            cumulative_share=tuple(params["cumulative_share"]), points=tuple(params["points"]),
            hipster_pick_points=params["hipster_pick_points"],
            max_bandwagon_answer_share=params["max_bandwagon_answer_share"],
            weak_answer_max_tier=params["weak_answer_max_tier"])

    @classmethod
    def from_tunables(cls, tunables: dict) -> "ModelParams":
        m, tiers = tunables["rarity"]["model"], tunables["tiers"]
        return cls(
            alpha=m["alpha"], beta=m["beta"], half_life_seasons=m["half_life_seasons"],
            fame_floor_views=m["fame_floor_views"], tier_names=tuple(tiers["names"]),
            cumulative_share=tuple(tiers["cumulative_share"]), points=tuple(tiers["points"]),
            hipster_pick_points=tiers["hipster_pick_points"],
            max_bandwagon_answer_share=tunables["rarity"]["tier_gate"]["max_bandwagon_answer_share"],
            weak_answer_max_tier=tunables["rarity"]["weak_answer_max_tier"])


@dataclass(frozen=True)
class RatedPrompt:
    scores: dict[int, float]
    shares: dict[int, float]
    tiers: dict[int, int]  # effective: 1 (Bandwagon) to 5, weak answers capped; points follow it
    model_tiers: dict[int, int]  # before the cap
    points: dict[int, int]
    gate: dict  # on the effective tiers
    model_gate: dict  # the same gate on the model tiers, to see what the cap costs
    expected_points: float
    log_recall_mass: float


def score(fame: float, tie: float, seasons_since: float, p: ModelParams) -> float:
    return (p.alpha * math.log(fame + p.fame_floor_views) + p.beta * math.log(1 + tie)
            - math.log(2) * seasons_since / p.half_life_seasons)


def softmax_shares(scores: dict[int, float]) -> dict[int, float]:
    """exp(s - max) / sum, summed with fsum in entity-id order so the floats are identical on every run."""
    if not scores:
        return {}
    top = max(scores.values())
    ids = sorted(scores)
    exps = {e: math.exp(scores[e] - top) for e in ids}
    total = math.fsum(exps[e] for e in ids)
    return {e: exps[e] / total for e in ids}


def assign_tiers(scores: dict[int, float], shares: dict[int, float], cumulative_share: tuple[float, ...]) -> dict[int, int]:
    """Midpoint tiers. Sorted by score descending then entity id; answers with exactly equal scores form a block
    that shares one tier. A block's midpoint is the cumulative share before it plus half its own share, and its
    tier is the first k with midpoint < cumulative_share[k] (1-based). Nothing is moved to fill an empty tier."""
    order = sorted(scores, key=lambda e: (-scores[e], e))
    tiers: dict[int, int] = {}
    before, i = 0.0, 0
    while i < len(order):
        j = i
        while j < len(order) and scores[order[j]] == scores[order[i]]:
            j += 1
        block = order[i:j]
        block_share = math.fsum(shares[e] for e in block)
        midpoint = before + block_share / 2
        tier = next((k + 1 for k, cut in enumerate(cumulative_share) if midpoint < cut), len(cumulative_share))
        for e in block:
            tiers[e] = tier
        before += block_share
        i = j
    return tiers


def effective_tiers(model_tiers: dict[int, int], weak: frozenset[int], p: ModelParams) -> dict[int, int]:
    """A weak answer is capped at weak_answer_max_tier; the rest keep the model's tier."""
    return {e: min(t, p.weak_answer_max_tier) if e in weak else t for e, t in model_tiers.items()}


def answer_points(tier: int, hipster: bool, p: ModelParams) -> int:
    return p.hipster_pick_points if hipster else p.points[tier - 1]


def tier_gate(tiers: dict[int, int], shares: dict[int, float], p: ModelParams) -> dict:
    """Passes when every tier is non-empty and Bandwagon holds at most max_bandwagon_answer_share of the
    answers. Floats are rounded for the jsonb column."""
    n = len(tiers)
    counts = [sum(t == k for t in tiers.values()) for k in range(1, len(p.tier_names) + 1)]
    cap = p.max_bandwagon_answer_share * n
    reasons = [f"empty tier: {name}" for name, c in zip(p.tier_names, counts) if c == 0]
    if counts[0] > cap + _EPS:
        reasons.append(f"Bandwagon too big: {counts[0]} of {n} answers (max {cap:.2f})")
    return {
        "tier_counts": counts, "n": n, "bandwagon_answers": counts[0],
        "max_bandwagon_answer_share": round(p.max_bandwagon_answer_share, _ROUND),
        "top_share": round(max(shares.values(), default=0.0), _ROUND),
        "n_eff": round(1 / math.fsum(s * s for _, s in sorted(shares.items())), _ROUND) if shares else 0.0,
        "reasons": reasons, "pass": not reasons}


def difficulty_seed(shares: dict[int, float], points: dict[int, int], scores: dict[int, float]) -> tuple[float, float]:
    """(expected_points, log_recall_mass): sum of share * points, and logsumexp of the scores. Step 4 maps
    these to a Glicko rating and to the chance of giving a valid answer."""
    ids = sorted(shares)
    expected = math.fsum(shares[e] * points[e] for e in ids)
    if not scores:
        return expected, -math.inf
    top = max(scores.values())
    return expected, top + math.log(math.fsum(math.exp(scores[e] - top) for e in sorted(scores)))


def rate_prompt(features: dict, p: ModelParams, hipster: frozenset[int] = frozenset(),
                weak: frozenset[int] = frozenset()) -> RatedPrompt:
    """Everything the model says about one prompt. features: entity_id -> Features (fame, tie_amount,
    seasons_since); weak: the answers flagged weak_stint."""
    scores = {e: score(f.fame, f.tie_amount, f.seasons_since, p) for e, f in features.items()}
    shares = softmax_shares(scores)
    model_tiers = assign_tiers(scores, shares, p.cumulative_share)
    tiers = effective_tiers(model_tiers, weak, p)
    points = {e: answer_points(t, e in hipster, p) for e, t in tiers.items()}
    expected, mass = difficulty_seed(shares, points, scores)
    return RatedPrompt(scores, shares, tiers, model_tiers, points, tier_gate(tiers, shares, p),
                       tier_gate(model_tiers, shares, p), expected, mass)


# ---------------------------------------------------------------------------
# Learning from picks (pure, not wired into anything yet)
# ---------------------------------------------------------------------------

def posterior_shares(predicted: dict[int, float], picks: dict[int, int], prior_weight: float) -> dict[int, float]:
    """(prior_weight * p + k) / (prior_weight + n): the posterior mean, with the prediction worth prior_weight
    picks. n counts only picks of answers in the set."""
    n = sum(picks.get(e, 0) for e in predicted)
    return {e: (prior_weight * p + picks.get(e, 0)) / (prior_weight + n) for e, p in predicted.items()}


def hipster_candidates(predicted: dict[int, float], posterior: dict[int, float], picks: dict[int, int],
                       base_tiers: dict[int, int], ratio: float, min_picks: int, min_tier: int) -> list[int]:
    """Answers whose posterior is at least ratio times the prediction, with at least min_picks picks, and whose
    base tier is at least min_tier. Entity ids, sorted."""
    return sorted(e for e in predicted
                  if posterior[e] >= ratio * predicted[e] and picks.get(e, 0) >= min_picks
                  and base_tiers[e] >= min_tier)
