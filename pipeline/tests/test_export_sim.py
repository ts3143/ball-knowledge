"""The simulation snapshot on placeholder rows (no database)."""

import json

from rarity.export_sim import build_snapshot, canonical, prompts_sha256
from rarity.model import MODEL_VERSION

RARITY = {"model_version": MODEL_VERSION, "reference_season": 2025, "pageviews_through": "2026-10-01",
          "expected_points": 27.5, "log_recall_mass": 3.25,
          "params": {"alpha": 0.6, "beta": 0.5, "points": [10, 25, 50, 75, 100],
                     "fame_weights": {"Q1": {"de": 1.0, "en": 0.4}}, "features": {"min_spell_seasons": 0.5}}}


def prompt_row(pid, comp=900, clubs=(12, 11)):
    return {"id": pid, "template": "t_a", "status": "gate_passed", "competition_id": comp, "club_ids": list(clubs),
            "answer_count": 2, "rarity": RARITY, "gate_results": {"tiers": {"top_share": 0.6, "n_eff": 1.8}}}


def answer_row(pid, eid, flags=()):
    return {"prompt_id": pid, "entity_id": eid, "score": -1.0 * eid, "predicted_share": 0.5, "model_tier": 2, "tier": 2,
            "points": 25, "hipster_pick": False, "flags": list(flags)}


def snapshot():
    return build_snapshot([prompt_row(2), prompt_row(1)], [answer_row(1, 8, ["weak_stint"]), answer_row(1, 7),
                                                           answer_row(2, 7), answer_row(2, 8)], "2030-01-01", "abc1234")


def test_snapshot_has_header_counts_sorted_ids_and_the_competition():
    s = snapshot()
    h = s["header"]
    assert (h["prompt_count"], h["answer_count"], h["model_version"], h["export_date"], h["git_commit"]) == (
        2, 4, MODEL_VERSION, "2030-01-01", "abc1234")
    assert [p["id"] for p in s["prompts"]] == [1, 2]
    p = s["prompts"][0]
    assert p["competition_id"] == 900 and p["club_ids"] == [11, 12] and p["top_share"] == 0.6
    assert [a["entity_id"] for a in p["answers"]] == [7, 8] and p["answers"][1]["weak"] and not p["answers"][0]["weak"]


def test_hash_covers_the_prompts_and_changes_with_them():
    s = snapshot()
    assert s["header"]["prompts_sha256"] == prompts_sha256(s["prompts"])
    s["prompts"][0]["answers"][0]["points"] = 100
    assert prompts_sha256(s["prompts"]) != s["header"]["prompts_sha256"]


def test_no_qids_or_fame_weights_are_exported():
    text = canonical(snapshot())
    assert "Q1" not in text and "fame_weights" not in text and "features" not in text
    assert json.loads(text)["prompts"][0]["rarity"]["params"]["points"] == [10, 25, 50, 75, 100]
