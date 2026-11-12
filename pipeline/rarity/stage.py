"""The rarity stage of `prompts generate`: rates one answer set from the evidence and returns what is stored,
and recomputes a stored prompt from its raw features and params (`rarity check`). Pure apart from one
read-only query; generate stays the only writer.
"""

from dataclasses import dataclass

from .features import AnswerSet, Evidence, Features, compute_features, fame
from .model import MODEL_VERSION, ROUND_DIGITS, WEAK_FLAG, ModelParams, RatedPrompt, rate_prompt

# prompt_answers columns the model writes, in the order they are stored and compared.
RARITY_COLUMNS = ("views_de", "views_en", "tie_amount", "seasons_since", "score", "predicted_share",
                  "model_tier", "tier", "points", "hipster_pick")


@dataclass(frozen=True)
class StoredRarity:
    answers: dict[int, tuple]  # entity_id -> values of RARITY_COLUMNS
    prompt: dict  # prompts.rarity
    gate: dict  # gate_results.tiers, on the effective tiers
    model_gate_pass: bool  # the same gate on the model's own tiers


def exact_floats(cur) -> None:
    """Makes the rest of this transaction read float8 back exactly: this server's default
    (extra_float_digits = 0) rounds to 15 digits, which would make a stored score differ from its recomputation."""
    cur.execute("set local extra_float_digits = 3")


def pageviews_through(cur) -> str | None:
    """The latest month of pageviews loaded, as the model's input date. `cur` must be a dict_row cursor."""
    cur.execute("select max(month) as m from entity_pageviews")
    month = cur.fetchone()["m"]
    return None if month is None else month.isoformat()


def stored_params(mp: ModelParams, ev: Evidence, tunables: dict) -> dict:
    """Everything a stored tier depends on: the model's parameters, the fame weights and the feature tunables."""
    p = ev.params
    return {**mp.to_params(), "fame_weights": dict(ev.fame_weights),
            "features": {"min_spell_seasons": p.min_spell_seasons, "imputed_apps_per_season": p.imputed_apps_per_season,
                         "edge_season_weight": p.edge_season_weight, "matches_per_season": p.matches_per_season,
                         "start_year": tunables["ingest"]["start_year"],
                         "open_ended_current_seasons": tunables["prompts"]["open_ended_current_seasons"]}}


def prompt_rarity(result: RatedPrompt, params: dict, reference_season: int, through: str | None) -> dict:
    return {"model_version": MODEL_VERSION, "params": params, "reference_season": reference_season,
            "pageviews_through": through, "expected_points": round(result.expected_points, ROUND_DIGITS),
            "log_recall_mass": round(result.log_recall_mass, ROUND_DIGITS)}


def answer_values(e: int, f: Features, r: RatedPrompt, hipster: bool = False) -> tuple:
    return (f.views_de, f.views_en, f.tie_amount, f.seasons_since, r.scores[e], r.shares[e], r.model_tiers[e],
            r.tiers[e], r.points[e], hipster)


def rate_for_storage(answer_set: AnswerSet, flags: dict[int, list[str]], ev: Evidence, mp: ModelParams,
                     params: dict, through: str | None) -> StoredRarity:
    features = compute_features(answer_set, ev)
    weak = frozenset(e for e in features if WEAK_FLAG in flags[e])
    r = rate_prompt(features, mp, weak=weak)
    return StoredRarity({e: answer_values(e, features[e], r) for e in features},
                        prompt_rarity(r, params, ev.params.reference_season, through), r.gate, r.model_gate["pass"])


def recompute(rarity: dict, rows: list[dict]) -> tuple[RatedPrompt, dict[int, tuple]]:
    """Rates a stored prompt from its stored raw features (views, tie, seasons since), its flags, hipster
    picks and params. rows: prompt_answers rows with the RARITY_COLUMNS and `flags`, keyed by entity_id."""
    params = rarity["params"]
    mp = ModelParams.from_params(params)
    weights = params["fame_weights"]
    features = {r["entity_id"]: Features(r["views_de"], r["views_en"], fame(r["views_de"], r["views_en"], weights),
                                         r["tie_amount"], r["seasons_since"]) for r in rows}
    hipster = frozenset(r["entity_id"] for r in rows if r["hipster_pick"])
    weak = frozenset(r["entity_id"] for r in rows if WEAK_FLAG in r["flags"])
    result = rate_prompt(features, mp, hipster=hipster, weak=weak)
    return result, {e: answer_values(e, features[e], result, e in hipster) for e in features}
