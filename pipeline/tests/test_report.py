import csv
import json
from datetime import date

from prompts.report import (
    Data,
    format_numbers,
    name_mismatch,
    render_evidence,
    template_numbers,
    write_review_files,
)
from prompts.templates import TEMPLATES


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


def fake_data(prompts, facts=None, mismatches=()):
    data = Data.__new__(Data)
    data.qg = {"max_answers": 300, "report_min_answers_alternatives": [1]}
    data.prompts, data.facts, data.weak_reasons, data.name_mismatches = prompts, facts or {}, {}, list(mismatches)
    return data


def prompt(pid, template, text, status, answers, mix="not_run"):
    return {"id": pid, "template": template, "params": {"k": pid}, "text": text, "status": status,
            "answer_count": len(answers), "answers": answers,
            "gate_results": {"size": {"n": len(answers), "min": 1, "max": 300, "pass": True}, "mix": mix,
                             "completeness": "not_run"}}


def answer(name, views, fact_ids=(), flags=()):
    return {"entity_id": hash(name) % 1000, "name": name, "views": views, "fact_ids": list(fact_ids),
            "flags": list(flags)}


def test_numbers_treat_a_mix_gate_that_did_not_run_as_not_passed():
    data = fake_data([prompt(1, "club_pair", "Old prompt", "gate_failed", [answer("A", 5)])])
    numbers = template_numbers(data, "club_pair")
    assert (numbers["generated"], numbers["too_few_obvious"], numbers["too_few_obscure"]) == (1, 0, 0)
    assert numbers["yield"] == {1: 1} and numbers["yield_mix"] == {1: 0}  # size-only yield; mix did not run


def test_review_files_list_only_passing_prompts_escape_pipes_and_csv_has_every_prompt(tmp_path):
    mix = {"obvious": 3, "obscure": 1, "min_obvious": 3, "min_obscure": 3, "pass": False, "advisory": True}
    fact = {"id": 7, "predicate": "played_for", "start_year": 2010, "end_year": 2012, "apps": 5,
            "source": "wikidata", "object_name": "Test Club"}
    data = fake_data(
        [prompt(1, "club_pair", "Passing prompt", "gate_passed",
                [answer("Pipe|Name", 10, [7], ["weak_stint"]), answer("Other", 20)], mix),
         prompt(2, "club_pair", "Failing prompt", "gate_failed", [answer("Nobody", 1)], mix)],
        facts={7: fact}, mismatches=[("Takuma ano", "Takuma Asano")])
    numbers = {t: template_numbers(data, t) for t in TEMPLATES}
    out = write_review_files(data, numbers, review_dir=tmp_path, today=date(2026, 1, 2))
    assert out == tmp_path / "2026-01-02"
    md = (out / "club_pair.md").read_text()
    assert "Passing prompt" in md and "Failing prompt" not in md and "Nobody" not in md
    assert "Pipe\\|Name" in md and "Test Club 2010–2012 5 apps (wikidata)" in md
    assert "Takuma ano" in (out / "summary.md").read_text()
    rows = list(csv.DictReader((out / "prompts.csv").open()))
    assert [r["text"] for r in rows] == ["Passing prompt", "Failing prompt"]
    assert json.loads(rows[0]["gate_results"])["mix"]["pass"] is False
    assert "mix (advisory): 3 obvious / 1 obscure, would fail" in md
    assert "advisory" in (out / "summary.md").read_text()
    assert {p.name for p in out.iterdir()} >= {"summary.md", "prompts.csv", "club_pair.md", "club_coaches.md"}


def test_numbers_label_mix_as_advisory_and_count_would_fail_among_passing():
    failing = {"obvious": 0, "obscure": 3, "min_obvious": 3, "min_obscure": 3, "pass": False, "advisory": True}
    data = fake_data([prompt(1, "club_pair", "P", "gate_passed", [answer("A", 5)], failing)])
    numbers = template_numbers(data, "club_pair")
    assert (numbers["passed"], numbers["mix_would_fail"], numbers["yield"], numbers["yield_mix"]) \
        == (1, 1, {1: 1}, {1: 0})
    assert "mix (advisory, not rejecting): would fail for 1 of 1" in "\n".join(format_numbers(numbers))
