import math
import statistics

import pytest

from rarity.calibrate import (
    Rated,
    concordance,
    group_stats,
    gut_report,
    gut_result,
    parse_sweep,
    read_gut,
    resolve_name,
)
from rarity.features import AnswerSet, Features
from rarity.model import ModelParams, rate_prompt
from rarity.preview import StoredPrompt

MP = ModelParams(alpha=0.8, beta=0.5, half_life_seasons=10, fame_floor_views=100,
                 tier_names=("Bandwagon", "Casual", "Real One", "Deep Cut", "You Know Ball"),
                 cumulative_share=(0.50, 0.75, 0.90, 0.98, 1.00), points=(10, 25, 50, 75, 100),
                 hipster_pick_points=15, max_bandwagon_answer_share=0.34, weak_answer_max_tier=3)

# placeholder names; fame falls with the id, so id 1 is the most obvious answer
NAMES = {1: "Anna Müller", 2: "Ben Placeholder", 3: "Cem Ö'Neil", 4: "Dora Dummy", 5: "Eli Example",
         6: "Fay Fixture", 7: "Gus Gamma", 8: "Hal Hotel", 9: "Ida India", 10: "Jon Juliet", 11: "Kim Kilo",
         12: "Lou Lima", 13: "Müller Zwei"}


def prompt(flags=None, mix_pass=True):
    answers = {i: [] for i in NAMES}
    return StoredPrompt(900, "club_pair", "Placeholder prompt", AnswerSet("club_pair", [], answers),
                        flags or {i: [] for i in NAMES}, NAMES, mix_pass)


def rated(p=None, mp=MP):
    p = p or prompt()
    feats = {i: Features(0, 0, 10.0 ** (8 - 0.5 * i), 5.0, 1.0) for i in NAMES}
    return Rated(p, feats, rate_prompt(feats, mp))


def test_resolve_name_matches_canonical_names_ignoring_accents_and_case():
    assert resolve_name("anna muller", NAMES, {}) == (1, "")
    assert resolve_name("Cem Oneil", NAMES, {}) == (3, "")


def test_resolve_name_falls_back_to_aliases():
    assert resolve_name("The Gamma", NAMES, {7: ["The Gamma"]}) == (7, "")


def test_unknown_and_ambiguous_names_are_errors_that_say_why():
    e, err = resolve_name("Nobody Here", NAMES, {})
    assert e is None and "not in the answer set" in err
    e, err = resolve_name("Mueller", NAMES, {13: ["Mueller"], 1: ["Mueller"]})
    assert e is None and "ambiguous" in err and "Anna Müller (id 1)" in err and "Müller Zwei (id 13)" in err
    # an exact canonical match wins over another entity's alias
    assert resolve_name("Anna Müller", NAMES, {2: ["Anna Muller"]}) == (1, "")


def test_concordance_counts_ordered_pairs_and_half_for_ties():
    scores = {1: 3.0, 2: 2.0, 3: 2.0, 4: 1.0}
    assert concordance(scores, [1, 2], [3, 4]) == (3.5, 4)  # 1>3, 1>4, 2=3 (half), 2>4
    assert concordance(scores, [4], [1]) == (0.0, 1)
    assert concordance(scores, [], [1]) == (0.0, 0)


def test_gut_result_overlaps_and_tier_misses():
    r = rated().result
    order = sorted(r.scores, key=lambda e: (-r.scores[e], e))  # 1, 2, 3, ... by construction
    assert order == list(NAMES)
    top, bottom = [1, 2, 3, 4, 12], [13, 12, 11, 10, 9]
    g = gut_result(r, top, bottom, 5)
    assert g.top_overlap == 4 and g.bottom_overlap == 5  # 12 is T's top but in the model's bottom 5
    assert g.points == 21 and g.pairs == 24  # 12 is below 4 of the 5 bottom answers and ties itself (skipped)
    assert g.top_deep >= 1 and g.concordance == pytest.approx(21 / 24)
    assert g.bottom_casual_or_shallower == 0


def test_read_gut_skips_empty_prompts_and_a_missing_file(tmp_path):
    assert read_gut(tmp_path / "missing.toml") == []
    path = tmp_path / "gut.toml"
    path.write_text('[[prompt]]\nid = 1\ntext = "a"\nanswers = ["x"]\ntop = []\nbottom = []\n'
                    '[[prompt]]\nid = 2\ntext = "b"\nanswers = ["x"]\ntop = ["x"]\nbottom = []\n')
    assert [e["id"] for e in read_gut(path)] == [2]
    path.write_text('[[prompt]]\nid = 1\ntext = "a"\nanswers = ["x"]\ntop = []\nbottom = []\n')
    assert read_gut(path) == []


def test_gut_report_says_so_when_nothing_is_filled_in(tmp_path):
    text = gut_report([], {}, {}, MP, tmp_path / "gut.toml")
    assert "no top or bottom list filled in yet" in text


def test_gut_report_scores_a_placeholder_sheet_and_continues_past_bad_names(tmp_path):
    r = rated()
    entries = [{"id": 900, "top": ["Anna Müller", "Ben Placeholder", "Nobody Here"], "bottom": ["Lou Lima", "Kim Kilo"]},
               {"id": 12345, "top": ["x"]}]
    text = gut_report(entries, {900: r}, {}, MP, tmp_path / "gut.toml")
    assert "ERROR top: 'Nobody Here' is not in the answer set" in text
    assert "[12345] not found in the database; skipped" in text
    assert "concordance 1.00 (4/4)" in text and "top-5 overlap 2/2" in text
    assert "OVERALL" in text and "bar met" in text


def test_gut_report_flags_a_top_answer_in_a_deep_tier(tmp_path):
    r = rated()
    entries = [{"id": 900, "top": ["Lou Lima"], "bottom": ["Anna Müller"]}]
    text = gut_report(entries, {900: r}, {}, MP, tmp_path / "gut.toml")
    assert "concordance 0.00" in text and "bar NOT met" in text and "top Lou Lima: You Know Ball" in text


def test_parse_sweep():
    assert parse_sweep(["alpha=0.6,0.8", "max_bandwagon_answer_share=0.3"]) == {
        "alpha": [0.6, 0.8], "max_bandwagon_answer_share": [0.3]}
    for bad in ("gamma=1", "alpha=", "alpha=x"):
        with pytest.raises(SystemExit):
            parse_sweep([bad])


def test_group_stats_counts_gate_passes_reasons_and_flagged_deep_answers():
    flags = {i: [] for i in NAMES}
    r = rated()
    deepest = max(r.result.tiers, key=lambda e: (r.result.tiers[e], e))
    flags[deepest] = ["weak_stint"]
    good = rated(prompt(flags, mix_pass=False))
    flat = {i: Features(0, 0, 1000.0, 5.0, 1.0) for i in NAMES}  # every score equal: one Casual block
    bad = Rated(prompt(), flat, rate_prompt(flat, MP))
    s = group_stats([good, bad], [10, 12], 12, 300, 5)
    assert s["yields"] == {10: (1, 2), 12: (1, 2)} and s["base"] == 2
    assert s["reasons"]["empty tier: Bandwagon"] == 1
    assert s["deep_flagged"] == 1 and s["deep_answers"] == good.result.gate["tier_counts"][3] + good.result.gate["tier_counts"][4]
    assert s["cross"][(False, True)] == 1 and s["cross"][(True, False)] == 1
    assert math.isclose(s["top_share"], statistics.median(r.result.gate["top_share"] for r in (good, bad)))


def test_gut_sweep_runs_every_combination_and_sorts_best_first():
    from rarity.calibrate import gut_sweep
    p = prompt()
    feats = {i: Features(0, 0, 10.0 ** (8 - 0.5 * i), 5.0, 1.0) for i in NAMES}
    entries = [{"id": 900, "top": ["Anna Müller", "Ben Placeholder", "Nobody Here"], "bottom": ["Lou Lima", "Kim Kilo"]}]
    tunables = {"quality_gates": {"min_answers": 12, "max_answers": 300}}
    grid = {"alpha": [0.8, 1.0], "beta": [0.5, 1.1]}
    rows = gut_sweep(entries, [p], {900: feats}, {}, tunables, MP, grid)
    assert len(rows) == 4
    assert {tuple(r["params"].items()) for r in rows} == {
        (("alpha", a), ("beta", b)) for a in (0.8, 1.0) for b in (0.5, 1.1)}
    for r in rows:
        assert r["gut"].pairs == 4 and r["gut"].concordance == 1.0 and r["gut"].top_size == 2
        assert r["sized"] == 1 and r["passed"] in (0, 1)
    # a prompt that is not in the database is reported, not fatal
    assert len(gut_sweep([{"id": 1, "top": ["x"]}], [p], {900: feats}, {}, tunables, MP, {"alpha": [0.8]})) == 1
    # sorted: flipping T's lists puts the worst ordering last
    worse = gut_sweep(entries + [{"id": 900, "top": ["Lou Lima"], "bottom": ["Anna Müller"]}], [p], {900: feats}, {},
                      tunables, MP, {"alpha": [0.8]})
    assert worse[0]["gut"].top_deep == 1
