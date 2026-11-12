import csv
import json
import tomllib
from datetime import date
from pathlib import Path

from prompts.report import (
    Data,
    _cell,
    format_numbers,
    name_mismatch,
    points_cell,
    prompt_header,
    render_evidence,
    template_numbers,
    write_review_files,
)
from prompts.templates import TEMPLATES
from rarity.model import ModelParams


def test_name_check_ignores_case_accents_and_a_trailing_parenthetical():
    assert not name_mismatch("Thomas MÜLLER", "Thomas Müller")
    assert not name_mismatch("Nuri Sahin", "Nuri Şahin")
    assert not name_mismatch("Thomas Müller", "Thomas_Müller_(footballer,_born_1989)")
    assert not name_mismatch("Max Eberl", "Max Eberl (footballer)")


def test_name_check_catches_a_mismatch():
    assert name_mismatch("Takuma ano", "Takuma Asano")
    assert name_mismatch("Takuma Asano", "Asano Takuma")
    assert name_mismatch("Max Eberl", "Max Eberl (footballer) Jr")


def test_render_evidence():
    stint = {"predicate": "played_for", "object_name": "Test Club A", "start_year": 2010, "end_year": 2012,
             "apps": 5, "source": "dewiki"}
    assert render_evidence(stint) == "Test Club A 2010–2012 5 apps (dewiki)"
    open_ended = {**stint, "end_year": None, "apps": None, "source": "wikidata"}
    assert render_evidence(open_ended) == "Test Club A 2010– (wikidata)"
    nationality = {"predicate": "nationality", "object_name": "Test Country", "start_year": None,
                   "end_year": None, "apps": None, "source": "wikidata"}
    assert render_evidence(nationality) == "nationality Test Country (wikidata)"


NAMES = ["Bandwagon", "Casual", "Real One", "Deep Cut", "You Know Ball"]
PARAMS = ModelParams.from_tunables(
    tomllib.loads((Path(__file__).resolve().parents[2] / "tunables.toml").read_text())).to_params()


def fake_data(prompts, facts=None, mismatches=()):
    data = Data.__new__(Data)
    data.qg = {"max_answers": 300, "report_min_answers_alternatives": [1]}
    data.tier_names = NAMES
    data.prompts, data.facts, data.weak_reasons, data.name_mismatches = prompts, facts or {}, {}, list(mismatches)
    return data


def answer(name, share=None, tier=None, model_tier=None, flags=(), fact_ids=()):
    rated = tier is not None
    return {"entity_id": sum(map(ord, name)), "name": name, "flags": list(flags), "fact_ids": list(fact_ids),
            "views_de": 1000 if rated else None, "views_en": 500 if rated else None, "tie": 12.0 if rated else None,
            "since": 2.0 if rated else None, "share": share, "tier": tier,
            "model_tier": model_tier if model_tier is not None else tier,
            "points": ([10, 25, 50, 75, 100][tier - 1] if tier <= 3 or tier == model_tier or model_tier is None
                       else 50) if rated else None}


def prompt(pid, template, text, status, answers, tier_gate=None):
    """A prompt with a tier-gate record built from its answers (None: not rated)."""
    n = len(answers)
    rated = tier_gate is not False and answers[0]["tier"] is not None
    counts = [sum(a["tier"] == k for a in answers) for k in range(1, 6)] if rated else None
    gate = "not_run"
    if rated:
        gate = {"tier_counts": counts, "n": n, "bandwagon_answers": counts[0], "max_bandwagon_answer_share": 0.34,
                "top_share": max(a["share"] for a in answers), "n_eff": 3.0,
                "reasons": [f"empty tier: {NAMES[k]}" for k in range(5) if counts[k] == 0],
                "pass": all(counts)}
    return {"id": pid, "template": template, "params": {"k": pid}, "text": text, "status": status,
            "answer_count": n, "answers": answers,
            "rarity": {"model_version": "rarity-1", "params": PARAMS, "expected_points": 27.5} if rated else None,
            "gate_results": {"size": {"n": n, "min": 1, "max": 300, "pass": True}, "tiers": gate,
                             "completeness": "not_run"}}


def five(prefix="P"):
    """Five answers, one per tier, in share order."""
    shares = [0.5, 0.25, 0.15, 0.08, 0.02]
    return [answer(f"{prefix}{k}", s, k + 1) for k, s in enumerate(shares)]


def test_review_files_list_only_passing_prompts_with_tiers_and_escape_pipes(tmp_path):
    fact = {"id": 7, "predicate": "played_for", "start_year": 2010, "end_year": 2012, "apps": 5,
            "source": "wikidata", "object_name": "Test Club"}
    answers = five()
    answers[2] = answer("Pipe|Name", 0.15, 3, flags=["weak_stint"], fact_ids=[7])
    answers[4] = answer("Capped", 0.02, 3, model_tier=5, flags=["weak_stint"])
    failing = [answer("Nobody", 1.0, 1)]
    data = fake_data([prompt(1, "club_pair", "Passing prompt", "gate_passed", answers),
                      prompt(2, "club_pair", "Failing prompt", "gate_failed", failing)],
                     facts={7: fact}, mismatches=[("Takuma ano", "Takuma Asano")])
    numbers = {t: template_numbers(data, t) for t in TEMPLATES}
    out = write_review_files(data, numbers, review_dir=tmp_path, today=date(2026, 1, 2))
    assert out == tmp_path / "2026-01-02"
    md = (out / "club_pair.md").read_text()
    assert "Passing prompt" in md and "Failing prompt" not in md and "Nobody" not in md
    assert "5 answers · tiers 1/1/3/0/0" not in md  # the tier column is per answer, counted below
    assert "| tier | points | share | name | de views | en views | tie | seasons since | flags | evidence |" in md
    assert "| Bandwagon | 10 | 50.0% | P0 | 1,000 | 500 | 12.0 | 2.0 |  |  |" in md
    assert "Pipe\\|Name" in md and "Test Club 2010–2012 5 apps (wikidata)" in md
    assert "| Real One | 50 (capped from You Know Ball) | 2.0% | Capped |" in md
    assert md.index("| P0 |") < md.index("| P1 |") < md.index("| Capped |")  # sorted by share
    assert "Takuma ano" in (out / "summary.md").read_text()
    assert {p.name for p in out.iterdir()} >= {"summary.md", "prompts.csv", "answers.csv", "club_pair.md"}


def test_prompt_header_and_points_cell():
    p = prompt(1, "club_pair", "P", "gate_passed", five())
    assert prompt_header(p) == "5 answers · tiers 1/1/1/1/1 · top 50% · expected 27.5 pts · tier gate pass"
    failing = prompt(2, "club_pair", "F", "gate_failed", [answer("A", 0.6, 1), answer("B", 0.4, 1)])
    assert prompt_header(failing).endswith("tier gate FAIL (empty tier: Casual; empty tier: Real One; "
                                           "empty tier: Deep Cut; empty tier: You Know Ball)")
    unrated = prompt(3, "club_pair", "U", "gate_failed", [answer("A")])
    assert prompt_header(unrated) == "1 answers · not rated (failed the size gate)"
    assert points_cell(answer("A", 0.1, 3, model_tier=5), NAMES) == "50 (capped from You Know Ball)"
    assert points_cell(answer("A", 0.1, 3), NAMES) == "50"


def test_csv_files_carry_the_rarity_fields(tmp_path):
    answers = five()
    answers[4] = answer("Capped", 0.02, 3, model_tier=5, flags=["weak_stint"])
    data = fake_data([prompt(1, "club_pair", "Passing prompt", "gate_passed", answers),
                      prompt(2, "club_pair", "Short prompt", "gate_failed", [answer("Nobody")])])
    out = write_review_files(data, {t: template_numbers(data, t) for t in TEMPLATES}, review_dir=tmp_path,
                             today=date(2026, 1, 2))
    rows = list(csv.DictReader((out / "prompts.csv").open()))
    assert [r["text"] for r in rows] == ["Passing prompt", "Short prompt"]
    first = rows[0]
    assert (first["model_version"], first["tier_counts"], first["top_share"], first["expected_points"]) \
        == ("rarity-1", "1/1/2/1/0", "0.5", "27.5")
    assert first["tier_gate_pass"] == "False" and first["tier_gate_reasons"] == "empty tier: You Know Ball"
    assert rows[1]["tier_counts"] == "" and rows[1]["tier_gate_pass"] == ""
    assert "mix" not in json.loads(first["gate_results"])
    answer_rows = list(csv.DictReader((out / "answers.csv").open()))
    assert len(answer_rows) == 5 and {r["prompt_id"] for r in answer_rows} == {"1"}  # only gate_passed prompts
    capped = answer_rows[-1]
    assert (capped["name"], capped["tier"], capped["tier_name"], capped["model_tier"], capped["points"]) \
        == ("Capped", "3", "Real One", "5", "50")
    assert capped["flags"] == "weak_stint" and capped["share"] == "0.02" and capped["template"] == "club_pair"
    assert capped["prompt"] == "Passing prompt" and capped["tie_amount"] == "12.0"


def test_numbers_on_placeholder_data():
    capped_answers = [answer("A", 0.5, 1), answer("B", 0.25, 2), answer("C", 0.15, 3),
                      answer("D", 0.08, 3, model_tier=4, flags=["weak_stint"]),
                      answer("E", 0.02, 3, model_tier=5, flags=["weak_stint"])]
    edge_answers = five("E")
    edge_answers[4] = answer("EdgeDeep", 0.02, 5, flags=["edge_season"])
    data = fake_data([
        prompt(1, "club_pair", "Cap loses", "gate_failed", capped_answers),
        prompt(2, "club_pair", "Passes", "gate_passed", edge_answers),
        prompt(3, "club_pair", "Flat", "gate_failed", [answer("X", 0.5, 1), answer("Y", 0.5, 1)]),
        prompt(4, "club_pair", "Short", "gate_failed", [answer("Z")])])
    data.prompts[3]["gate_results"]["size"]["pass"] = False
    n = template_numbers(data, "club_pair")
    assert (n["generated"], n["passed"], n["size_passed"], n["tier_failed"]) == (4, 1, 3, 2)
    assert n["weak_cap_lost"] == 1  # prompt 1 fills five tiers on model tiers; prompt 3 fails either way
    assert (n["tier_reasons"]["empty tier: You Know Ball"], n["tier_reasons"]["empty tier: Casual"]) == (2, 1)
    assert n["tier_counts"] == [1, 1, 1, 1, 1] and n["top_share"] == 0.5 and n["expected_points"] == 27.5
    assert (n["answers"], n["weak"], n["edge"]) == (5, 0, 1)
    assert (n["deep"], n["deep_flagged"]) == (2, 1)  # You Know Ball and Deep Cut; the edge-flagged one counts
    assert n["yield"] == {1: 4}
    text = "\n".join(format_numbers(n))
    assert "tier gate 2" in text and "lost to the weak-answer cap: 1" in text
    assert "weak or edge-flagged answers in Deep Cut or You Know Ball: 1 of 2" in text
    assert "tiers 1/1/1/1/1" in text and "top share 50.0%" in text


def test_no_mix_gate_is_left_in_the_gate_results_or_the_tunables():
    from prompts.gates import gate_results
    assert "mix" not in gate_results(20, {"min_answers": 12, "max_answers": 300})
    root = Path(__file__).resolve().parents[2]
    tunables = tomllib.loads((root / "tunables.toml").read_text())
    assert not [k for k in tunables["quality_gates"] if k.startswith("mix")]
    assert "mix" not in (root / "tunables.toml").read_text().lower().replace("mixed", "")


def test_table_cells_escape_backslashes_and_pipes_and_flatten_newlines():
    assert _cell("a|b") == "a\\|b"
    assert _cell("a\\|b") == "a\\\\\\|b"  # a literal backslash, then an escaped pipe
    assert _cell("line one\nline two\r\nthree") == "line one line two three"
