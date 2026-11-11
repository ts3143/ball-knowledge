"""Prompt generation on placeholder rows inside a rolled-back transaction. Needs the local database."""

from datetime import date

import psycopg
import pytest
from psycopg.rows import dict_row

from ingest.config import load_settings
from prompts.generate import generate

pytestmark = pytest.mark.db

TUNABLES = {
    "ingest": {"start_year": 2000, "coverage": {"teams_per_season": 3}},
    "quality_gates": {"min_answers": 1, "max_answers": 300, "report_min_answers_alternatives": [1, 2]},
}

# Placeholder seasons (start year -> clubs): 2009 sees C relegated and D promoted, 2011 sees D relegated
# and C promoted. 2012 lists only two clubs, so nothing can be decided about it; 2013 lists E as new, but
# 2012 is not fully listed.
SEASONS = {2008: "ABC", 2009: "ABD", 2010: "ABD", 2011: "ABC", 2012: "AB", 2013: "ABE"}


@pytest.fixture
def conn():
    try:
        conn = psycopg.connect(load_settings().database_url, connect_timeout=3)
    except (psycopg.OperationalError, SystemExit) as exc:
        pytest.skip(f"local database not reachable: {exc}")
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


class World:
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor(row_factory=dict_row)
        self.n = 0
        self.comp = self.entity("competition", "Test League")
        self.clubs = {c: self.entity("club", f"Test Club {c}") for c in "ABCDE"}
        self.country = self.entity("country", "Test Country")
        for year, letters in SEASONS.items():
            for letter in letters:
                self.fact(self.clubs[letter]["id"], "competed_in", self.comp["id"], start_year=year,
                          end_year=year + 1, start_date=date(year, 8, 1), end_date=date(year + 1, 5, 31))

    def entity(self, type_, name):
        self.n += 1
        return self.cur.execute(
            "insert into entities (type, wikidata_id, canonical_name) values (%s, %s, %s)"
            " returning id, wikidata_id, canonical_name",
            (type_, f"Q99100{self.n:04d}", name)).fetchone()

    def fact(self, subject, predicate, obj, stale=False, **cols):
        self.n += 1
        cols = {"date_quality": "dated", **cols}
        return self.cur.execute(
            f"insert into facts (subject_id, predicate, object_id, stale, source_ref, {', '.join(cols)})"
            f" values (%s, %s, %s, %s, %s, {', '.join(['%s'] * len(cols))}) returning id",
            (subject, predicate, obj, stale, f"test:gen:{self.n}", *cols.values())).fetchone()["id"]

    def stint(self, person, club, predicate="played_for", apps=5, **cols):
        cols = {"start_year": 2010, "end_year": 2012, "start_precision": 9, "end_precision": 9, **cols}
        return self.fact(person["id"], predicate, self.clubs[club]["id"], apps=apps, **cols)

    def person(self, name):
        return self.entity("person", name)

    def run(self, *templates, dry_run=False, **overrides):
        tunables = {**TUNABLES, **overrides}
        return generate(self.conn, self.comp["wikidata_id"], tunables, list(templates) or None, dry_run)

    def prompts(self, template):
        return self.cur.execute(
            "select * from prompts where template = %s and competition_id = %s order by id",
            (template, self.comp["id"])).fetchall()

    def answers(self, prompt_id):
        rows = self.cur.execute("select * from prompt_answers where prompt_id = %s", (prompt_id,)).fetchall()
        return {r["entity_id"]: r for r in rows}

    def only(self, template, **params):
        found = [p for p in self.prompts(template) if all(p["params"][k] == v for k, v in params.items())]
        assert len(found) == 1, found
        return found[0]


@pytest.fixture
def world(conn):
    return World(conn)


def pair_params(w, a, b):
    return {"club_a": w.clubs[a]["wikidata_id"], "club_b": w.clubs[b]["wikidata_id"]}


def test_club_pair_needs_both_clubs(world):
    w = world
    p1, p2, p3 = w.person("P1"), w.person("P2"), w.person("P3")
    w.stint(p1, "A")
    w.stint(p1, "B")
    w.stint(p2, "A")
    w.stint(p3, "B")
    w.run("club_pair")
    prompt = w.only("club_pair", **pair_params(w, "A", "B"))
    assert set(w.answers(prompt["id"])) == {p1["id"]}
    assert prompt["club_ids"] == [w.clubs["A"]["id"], w.clubs["B"]["id"]]
    assert prompt["text"] == "Name a player who played for both Test Club A and Test Club B since 2000"
    assert len(w.prompts("club_pair")) == 1  # pairs with no shared player are not enumerated


def test_club_pair_canonical_order_is_by_numeric_qid(world):
    w = world
    p1 = world.person("P1")
    w.stint(p1, "B")
    w.stint(p1, "A")
    w.run("club_pair")
    params = w.prompts("club_pair")[0]["params"]
    assert (params["club_a"], params["club_b"]) == (w.clubs["A"]["wikidata_id"], w.clubs["B"]["wikidata_id"])


def test_stale_facts_are_excluded(world):
    w = world
    p1 = w.person("P1")
    w.stint(p1, "A")
    w.stint(p1, "B", stale=True)
    w.run("club_pair")
    assert w.prompts("club_pair") == []


def test_weak_stint_needs_every_stint_at_a_club_to_be_weak(world):
    w = world
    strong, mixed, weak_at_b = w.person("Strong"), w.person("Mixed"), w.person("WeakAtB")
    for p in (strong, mixed, weak_at_b):
        w.stint(p, "A")
    w.stint(strong, "B")
    w.stint(mixed, "A", apps=None, start_year=2008, end_year=2009)  # weak stint, but a strong one exists too
    w.stint(mixed, "B")
    w.stint(weak_at_b, "B", apps=0)
    w.run("club_pair")
    answers = w.answers(w.only("club_pair", **pair_params(w, "A", "B"))["id"])
    assert answers[strong["id"]]["flags"] == []
    assert answers[mixed["id"]]["flags"] == []
    assert answers[weak_at_b["id"]]["flags"] == ["weak_stint"]


def test_evidence_lists_the_stint_facts(world):
    w = world
    p1 = w.person("P1")
    fa, fb = w.stint(p1, "A"), w.stint(p1, "B")
    w.run("club_pair")
    answers = w.answers(w.only("club_pair", **pair_params(w, "A", "B"))["id"])
    assert answers[p1["id"]]["evidence_fact_ids"] == [fa, fb]


def test_club_nationality_joins_citizenship_and_club(world):
    w = world
    citizen, weak_citizen, other = w.person("Citizen"), w.person("WeakCitizen"), w.person("Other")
    other_country = w.entity("country", "Other Country")
    for p in (citizen, weak_citizen, other):
        w.stint(p, "A")
    w.stint(weak_citizen, "A", apps=None)
    w.stint(citizen, "B")
    nat = w.fact(citizen["id"], "nationality", w.country["id"])
    w.fact(weak_citizen["id"], "nationality", w.country["id"])
    w.fact(other["id"], "nationality", other_country["id"])
    w.fact(citizen["id"], "nationality", other_country["id"], stale=True)
    w.run("club_nationality")
    prompt = w.only("club_nationality", country=w.country["wikidata_id"], club=w.clubs["A"]["wikidata_id"])
    answers = w.answers(prompt["id"])
    assert set(answers) == {citizen["id"], weak_citizen["id"]}
    assert nat in answers[citizen["id"]]["evidence_fact_ids"]
    assert answers[citizen["id"]]["flags"] == []
    assert prompt["text"] == ("Name a player with Test Country citizenship who played for Test Club A"
                              " since 2000")
    assert prompt["club_ids"] == [w.clubs["A"]["id"]]
    # Club B has a citizen too, and the other country's only citizen is a club A player.
    assert len(w.prompts("club_nationality")) == 3


def test_club_coaches(world):
    w = world
    coach, undated, player = w.person("Coach"), w.person("Undated"), w.person("Player")
    w.stint(coach, "A", predicate="coached", apps=None)
    w.fact(undated["id"], "coached", w.clubs["A"]["id"], date_quality="undated")
    w.stint(player, "A")
    w.run("club_coaches")
    prompt = w.only("club_coaches", club=w.clubs["A"]["wikidata_id"])
    answers = w.answers(prompt["id"])
    assert set(answers) == {coach["id"], undated["id"]}
    assert answers[coach["id"]]["flags"] == []
    assert answers[undated["id"]]["flags"] == ["weak_stint"]
    assert prompt["text"] == "Name a head coach of Test Club A since 2000 (interim coaches count)"
    assert prompt["answer_type"] == "person"


def test_relegated_clubs_from_season_gaps(world):
    w = world
    w.run("relegated_clubs")
    [prompt] = w.prompts("relegated_clubs")
    assert prompt["params"] == {"competition": w.comp["wikidata_id"], "from_year": 2009, "to_year": 2011}
    assert prompt["text"] == "Name a club relegated from the Test League between 2009 and 2011"
    assert prompt["answer_type"] == "club"
    assert prompt["club_ids"] == []
    # C left after 2008-09, D after 2010-11. C's absence from 2012-13 is undecidable: 2012-13 is not fully listed.
    assert set(w.answers(prompt["id"])) == {w.clubs["C"]["id"], w.clubs["D"]["id"]}


def test_promoted_clubs_from_season_gaps(world):
    w = world
    w.run("promoted_clubs")
    [prompt] = w.prompts("promoted_clubs")
    assert prompt["params"] == {"competition": w.comp["wikidata_id"], "from_year": 2009, "to_year": 2011}
    assert prompt["text"] == "Name a club promoted to the Test League between 2009 and 2011"
    # D came up in 2009, C in 2011. E (2013-14) is undecidable: 2012-13 is not fully listed.
    assert set(w.answers(prompt["id"])) == {w.clubs["C"]["id"], w.clubs["D"]["id"]}


def test_no_moves_when_the_next_season_is_not_fully_listed(world):
    w = world
    w.run("relegated_clubs", "promoted_clubs", ingest={"start_year": 2000, "coverage": {"teams_per_season": 4}})
    assert w.prompts("relegated_clubs") == [] and w.prompts("promoted_clubs") == []


def test_answer_query_reproduces_the_answer_set(world):
    w = world
    p1, p2 = w.person("P1"), w.person("P2")
    for p in (p1, p2):
        w.stint(p, "A")
        w.stint(p, "B")
    w.run("club_pair", "relegated_clubs")
    for template in ("club_pair", "relegated_clubs"):
        [prompt] = w.prompts(template)
        rerun = w.cur.execute(prompt["answer_query"]).fetchall()
        assert {r["entity_id"] for r in rerun} == set(w.answers(prompt["id"]))


def test_gate_status_and_counts(world):
    w = world
    p1, p2 = w.person("P1"), w.person("P2")
    for p in (p1, p2):
        w.stint(p, "A")
        w.stint(p, "B")
    w.stint(p1, "A")
    stats = w.run("club_pair", quality_gates={**TUNABLES["quality_gates"], "min_answers": 3})["club_pair"]
    prompt = w.prompts("club_pair")[0]
    assert prompt["status"] == "gate_failed"
    assert prompt["gate_results"]["size"] == {"n": 2, "min": 3, "max": 300, "pass": False}
    assert prompt["answer_count"] == 2
    assert (stats.generated, stats.gate_passed, stats.too_few, stats.too_many) == (1, 0, 1, 0)
    assert stats.pass_at_min == {1: 1, 2: 1}


def test_second_run_changes_nothing(world):
    w = world
    p1 = w.person("P1")
    w.stint(p1, "A")
    w.stint(p1, "B")
    w.fact(p1["id"], "nationality", w.country["id"])
    first = w.run()
    assert sum(s.inserted for s in first.values()) > 0
    before = w.cur.execute("select id, updated_at from prompts where competition_id = %s order by id",
                           (w.comp["id"],)).fetchall()
    second = w.run()
    assert [(s.inserted, s.updated, s.deleted) for s in second.values()] == [(0, 0, 0)] * len(second)
    assert sum(s.unchanged for s in second.values()) == sum(s.inserted for s in first.values())
    assert before == w.cur.execute("select id, updated_at from prompts where competition_id = %s order by id",
                                   (w.comp["id"],)).fetchall()


def test_candidate_is_updated_when_the_answer_set_changes(world):
    w = world
    p1, p2 = w.person("P1"), w.person("P2")
    w.stint(p1, "A")
    w.stint(p1, "B")
    w.run("club_pair")
    w.stint(p2, "A")
    w.stint(p2, "B")
    stats = w.run("club_pair")["club_pair"]
    assert (stats.inserted, stats.updated, stats.deleted) == (0, 1, 0)
    assert set(w.answers(w.prompts("club_pair")[0]["id"])) == {p1["id"], p2["id"]}


def test_approved_prompt_is_not_rewritten_and_its_diff_is_reported(world):
    w = world
    p1, p2 = w.person("P1"), w.person("P2")
    w.stint(p1, "A")
    w.stint(p1, "B")
    w.run("club_pair")
    prompt = w.prompts("club_pair")[0]
    w.cur.execute("update prompts set status = 'approved' where id = %s", (prompt["id"],))
    w.stint(p2, "A")
    w.stint(p2, "B")
    w.cur.execute("delete from facts where subject_id = %s and predicate = 'played_for'", (p1["id"],))
    approved_before = w.prompts("club_pair")[0]
    answers_before = w.answers(prompt["id"])

    stats = w.run("club_pair")["club_pair"]

    assert (stats.inserted, stats.updated, stats.deleted, stats.approved) == (0, 0, 0, 1)
    [diff] = stats.approved_diffs
    assert (diff.prompt_id, diff.added, diff.removed) == (prompt["id"], ["P2"], ["P1"])
    assert w.prompts("club_pair")[0] == approved_before
    assert w.answers(prompt["id"]) == answers_before


def test_approved_prompt_survives_when_no_longer_produced(world):
    w = world
    p1 = w.person("P1")
    f = w.stint(p1, "B")
    w.stint(p1, "A")
    w.run("club_pair")
    w.cur.execute("update prompts set status = 'approved' where template = 'club_pair'")
    w.cur.execute("delete from facts where id = %s", (f,))
    stats = w.run("club_pair")["club_pair"]
    assert (stats.deleted, stats.approved_gone) == (0, 1)
    assert len(w.prompts("club_pair")) == 1


def test_candidate_no_longer_produced_is_deleted(world):
    w = world
    p1 = w.person("P1")
    w.stint(p1, "A")
    f = w.stint(p1, "C")
    w.run("club_pair")
    assert len(w.prompts("club_pair")) == 1
    w.cur.execute("delete from facts where id = %s", (f,))
    stats = w.run("club_pair")["club_pair"]
    assert stats.deleted == 1
    assert w.prompts("club_pair") == []


def test_dry_run_writes_nothing(world):
    w = world
    p1 = w.person("P1")
    w.stint(p1, "A")
    w.stint(p1, "B")
    stats = w.run("club_pair", dry_run=True)["club_pair"]
    assert stats.inserted == 1
    assert w.prompts("club_pair") == []
