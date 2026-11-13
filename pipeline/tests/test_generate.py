"""Prompt generation on placeholder rows inside a rolled-back transaction. Needs the local database."""

from datetime import date

import psycopg
import pytest
from psycopg.types.json import Jsonb

from prompts.generate import generate
from tests.db_world import TUNABLES, World

pytestmark = pytest.mark.db


def stint_without_start(w, person, club, predicate="played_for", end_year=2012, **cols):
    """A spell that ends at a known year but has no start year."""
    return w.stint(person, club, predicate=predicate, start_year=None, start_precision=None, end_year=end_year,
                   **cols)


def stint_out_of_the_league(w, person, club, predicate="played_for", **cols):
    """A month-precision spell between two seasons of club C (2008-09 and 2011-12): 2009-09 to 2010-05."""
    return w.stint(person, club, predicate=predicate, start_year=2009, start_date=date(2009, 9, 1),
                   start_precision=11, end_year=2010, end_date=date(2010, 5, 31), end_precision=11, **cols)


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


def test_excluded_countries_never_become_a_parameter(world):
    w = world
    former = w.entity("country", "Former Country")
    p = w.person("Citizen")
    w.stint(p, "A")
    w.fact(p["id"], "nationality", w.country["id"])
    w.fact(p["id"], "nationality", former["id"])
    w.run("club_nationality", "bundesliga_nationality",
          prompts={**TUNABLES["prompts"], "excluded_countries": [former["wikidata_id"]]})
    for template in ("club_nationality", "bundesliga_nationality"):
        countries = {r["params"]["country"] for r in w.prompts(template)}
        assert countries == {w.country["wikidata_id"]}, template


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


def test_current_from_year_comes_from_the_latest_season_and_the_tunable(world):
    w = world
    current, recent, older = w.person("Current"), w.person("Recent"), w.person("Older")
    for coach, start in ((current, 2012), (recent, 2011), (older, 2008)):
        w.stint(coach, "A", predicate="coached", date_quality="open_ended", start_year=start, end_year=None,
                end_precision=None)
    w.run("club_coaches")
    prompt = w.only("club_coaches", club=w.clubs["A"]["wikidata_id"])
    # Latest placeholder season is 2013; two seasons count as current, so from 2012.
    assert "stints(2012)" in prompt["answer_query"]
    answers = w.answers(prompt["id"])
    assert answers[current["id"]]["flags"] == []
    assert answers[recent["id"]]["flags"] == ["weak_stint"]
    assert answers[older["id"]]["flags"] == ["weak_stint"]
    w.run("club_coaches", prompts={"open_ended_current_seasons": 3})
    prompt = w.only("club_coaches", club=w.clubs["A"]["wikidata_id"])
    assert "stints(2011)" in prompt["answer_query"]
    assert w.answers(prompt["id"])[recent["id"]]["flags"] == []
    assert w.answers(prompt["id"])[older["id"]]["flags"] == ["weak_stint"]


def test_bundesliga_nationality_skips_undated_and_flags_edge_and_weak(world):
    w = world
    strong, edge_only, undated_too, weak, only_undated, other = (
        w.person(n) for n in ("Strong", "EdgeOnly", "UndatedToo", "Weak", "OnlyUndated", "Other"))
    other_country, undated_country = w.entity("country", "Other Country"), w.entity("country", "Undated Country")
    nat = {p["id"]: w.fact(p["id"], "nationality", w.country["id"])
           for p in (strong, edge_only, undated_too, weak, only_undated)}
    w.fact(other["id"], "nationality", other_country["id"])
    w.stint(strong, "A")  # 2010-2012: 2010 and 2011 certain, 2009 and 2012 edges
    w.stint(edge_only, "A", start_year=2011, end_year=2011)  # only edge seasons (2010, 2011)
    w.stint(undated_too, "A", start_year=2011, end_year=2011)
    w.fact(undated_too["id"], "played_for", w.clubs["B"]["id"], apps=5, date_quality="undated")
    w.stint(weak, "A", apps=None)
    w.fact(only_undated["id"], "played_for", w.clubs["A"]["id"], apps=5, date_quality="undated")
    w.fact(only_undated["id"], "nationality", undated_country["id"])
    w.stint(other, "A")
    w.run("bundesliga_nationality")
    prompt = w.only("bundesliga_nationality", country=w.country["wikidata_id"])
    answers = w.answers(prompt["id"])
    assert set(answers) == {strong["id"], edge_only["id"], undated_too["id"], weak["id"]}
    assert answers[strong["id"]]["flags"] == []
    assert answers[edge_only["id"]]["flags"] == ["edge_season"]
    # Its undated spell at B is skipped: only the edge-only dated spell counts.
    assert answers[undated_too["id"]]["flags"] == ["edge_season"]
    assert answers[weak["id"]]["flags"] == ["weak_stint"]
    assert nat[strong["id"]] in answers[strong["id"]]["evidence_fact_ids"]
    assert prompt["text"] == ("Name a player with Test Country citizenship who played in the Test League"
                              " since 2000")
    assert prompt["club_ids"] == []
    # Countries enumerated only with a dated-season answer: not the undated-only one.
    assert len(w.prompts("bundesliga_nationality")) == 2


def test_club_title_winners_need_a_stint_covering_a_won_season(world):
    w = world
    winner, edge, late, other_club, undated, weak = (
        w.person(n) for n in ("Winner", "Edge", "Late", "OtherClub", "Undated", "Weak"))
    won = w.fact(w.clubs["A"]["id"], "won", w.comp["id"], start_year=2010, end_year=2011)
    w.fact(w.clubs["A"]["id"], "won", w.comp["id"], stale=True, start_year=2012, end_year=2013)
    w.stint(winner, "A")  # 2010-2012 covers 2010 for certain
    w.stint(edge, "A", start_year=2011, end_year=2013)  # 2011-2013 reaches 2010-11 only as an edge: summer rule
    loan = w.person("Loan")
    w.stint(loan, "A", start_year=2011, end_year=2011)  # a January-June 2011 spell may be in the 2010-11 title
    w.stint(late, "A", start_year=2012, end_year=2013)
    w.stint(other_club, "B")
    w.fact(undated["id"], "played_for", w.clubs["A"]["id"], apps=5, date_quality="undated")
    w.stint(weak, "A", apps=None)
    w.run("club_title_winners")
    [prompt] = w.prompts("club_title_winners")  # only A has a (non-stale) won fact
    answers = w.answers(prompt["id"])
    assert set(answers) == {winner["id"], loan["id"], weak["id"]}  # not the 2011-2013 spell
    assert answers[winner["id"]]["flags"] == []
    assert answers[loan["id"]]["flags"] == ["edge_season"]  # same-year spell: kept, but only possibly a winner
    assert edge["id"] not in answers  # the Fabian Johnson pattern: a multi-year spell starting in the title's second year
    assert answers[weak["id"]]["flags"] == ["weak_stint"]
    assert won in answers[winner["id"]]["evidence_fact_ids"]
    assert prompt["params"] == {"club": w.clubs["A"]["wikidata_id"]}
    assert prompt["text"] == "Name a player who won the Test League with Test Club A since 2000"
    assert prompt["club_ids"] == [w.clubs["A"]["id"]]


def test_multi_club_coaches_need_two_clubs_with_dated_seasons(world):
    w = world
    two, edge, undated_second, one, weak = (w.person(n) for n in ("Two", "Edge", "UndatedSecond", "One", "Weak"))
    w.stint(two, "A", predicate="coached", apps=None, start_year=2009, end_year=2011)
    w.stint(two, "B", predicate="coached", apps=None)
    w.stint(edge, "A", predicate="coached", apps=None, start_year=2009, end_year=2011)
    w.stint(edge, "B", predicate="coached", apps=None, start_year=2011, end_year=2011)  # edge seasons only
    w.stint(undated_second, "A", predicate="coached", apps=None)
    w.fact(undated_second["id"], "coached", w.clubs["B"]["id"], date_quality="undated")
    w.stint(one, "A", predicate="coached", apps=None)
    w.stint(one, "A", predicate="coached", apps=None, start_year=2008, end_year=2009)
    w.stint(weak, "A", predicate="coached", apps=None, start_year=2009, end_year=2011)
    w.stint(weak, "B", predicate="coached", apps=None, date_quality="open_ended", start_year=2009, end_year=None,
            end_precision=None)
    w.run("multi_club_coaches")
    [prompt] = w.prompts("multi_club_coaches")
    answers = w.answers(prompt["id"])
    assert set(answers) == {two["id"], edge["id"], weak["id"]}
    assert answers[two["id"]]["flags"] == []
    assert answers[edge["id"]]["flags"] == ["edge_season"]
    assert answers[weak["id"]]["flags"] == ["weak_stint"]
    assert prompt["params"] == {"competition": w.comp["wikidata_id"]}
    assert prompt["text"] == "Name a coach who managed at least two Test League clubs since 2000"
    assert prompt["club_ids"] == []


def test_multi_club_coaches_not_generated_without_a_qualifying_coach(world):
    w = world
    w.stint(w.person("One"), "A", predicate="coached")
    w.run("multi_club_coaches")
    assert w.prompts("multi_club_coaches") == []


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
    p1, p2, winner = w.person("P1"), w.person("P2"), w.person("Winner")
    for p in (p1, p2):
        w.stint(p, "A")
        w.stint(p, "B")
    w.stint(p2, "B", apps=None)
    w.fact(w.clubs["A"]["id"], "won", w.comp["id"], start_year=2010, end_year=2011)
    w.stint(winner, "A", start_year=2011, end_year=2011)  # a half-season spell reaches the 2010 title as an edge
    w.run("club_pair", "relegated_clubs", "club_title_winners")
    for template in ("club_pair", "relegated_clubs", "club_title_winners"):
        [prompt] = w.prompts(template)
        rerun = {r["entity_id"]: (r["evidence_fact_ids"], r["flags"])
                 for r in w.cur.execute(prompt["answer_query"]).fetchall()}
        stored = {e: (r["evidence_fact_ids"], r["flags"]) for e, r in w.answers(prompt["id"]).items()}
        assert rerun == stored
    # The season template's flags and evidence are really in what was compared.
    [titles] = w.prompts("club_title_winners")
    assert w.answers(titles["id"])[winner["id"]]["flags"] == ["edge_season"]
    assert len(w.answers(titles["id"])[winner["id"]]["evidence_fact_ids"]) == 2


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


def add_views(w, entity, *views):
    for i, v in enumerate(views):
        w.cur.execute("insert into entity_pageviews (entity_id, wiki, month, views) values (%s, %s, %s, %s)",
                      (entity["id"], "de" if i % 2 else "en", date(2024, 1 + i // 2, 1), v))


def test_gate_results_hold_size_tiers_and_completeness_only(world):
    w = world
    for i in range(3):
        w.stint(w.person(f"Coach A{i}"), "A", predicate="coached")
    w.run("club_coaches")
    [prompt] = w.prompts("club_coaches")
    assert list(prompt["gate_results"]) == ["size", "tiers", "completeness"]
    assert prompt["gate_results"]["completeness"] == "not_run"
    assert prompt["gate_results"]["tiers"]["n"] == 3  # rated: it passes the size gate (min 1 here)


def test_template_filter_does_not_change_the_gate_results(world):
    w = world
    for i in range(4):
        coach = w.person(f"Coach {i}")
        w.stint(coach, "A", predicate="coached")
        if i:
            add_views(w, coach, 10 * i)
    w.stint(w.person("Other"), "B", predicate="coached")
    w.run("club_coaches")
    only_one = [p["gate_results"] for p in w.prompts("club_coaches")]
    w.run()
    assert [p["gate_results"] for p in w.prompts("club_coaches")] == only_one


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
    w.cur.execute("update prompts set status = 'approved' where template = 'club_pair' and competition_id = %s",
                  (w.comp["id"],))
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


def test_end_only_stint_counts_in_club_pair_flagged_weak(world):
    w = world
    p1 = w.person("P1")
    stint_without_start(w, p1, "A")
    w.stint(p1, "B")
    w.run("club_pair")
    answers = w.answers(w.only("club_pair", **pair_params(w, "A", "B"))["id"])
    assert answers[p1["id"]]["flags"] == ["weak_stint"]


def test_end_only_stint_is_not_a_season_template_answer(world):
    w = world
    winner, end_only, citizen, end_only_citizen = (
        w.person(n) for n in ("Winner", "EndOnly", "Citizen", "EndOnlyCitizen"))
    w.fact(w.clubs["A"]["id"], "won", w.comp["id"], start_year=2010, end_year=2011)
    w.stint(winner, "A")
    stint_without_start(w, end_only, "A")  # ends 2012; with start -infinity it would cover the 2010 title
    for p in (citizen, end_only_citizen):
        w.fact(p["id"], "nationality", w.country["id"])
    w.stint(citizen, "A")
    stint_without_start(w, end_only_citizen, "A")
    two, one_end_only = w.person("Two"), w.person("OneEndOnly")
    w.stint(two, "A", predicate="coached", apps=None, start_year=2009, end_year=2011)
    w.stint(two, "B", predicate="coached", apps=None)
    w.stint(one_end_only, "A", predicate="coached", apps=None, start_year=2009, end_year=2011)
    stint_without_start(w, one_end_only, "B", predicate="coached", apps=None)
    w.run("club_title_winners", "bundesliga_nationality", "multi_club_coaches")
    [titles] = w.prompts("club_title_winners")
    assert set(w.answers(titles["id"])) == {winner["id"], citizen["id"]}  # not the end-only spells
    nationality = w.only("bundesliga_nationality", country=w.country["wikidata_id"])
    assert set(w.answers(nationality["id"])) == {citizen["id"]}
    [coaches] = w.prompts("multi_club_coaches")
    assert set(w.answers(coaches["id"])) == {two["id"]}


def test_a_spell_while_the_club_is_out_of_the_league_is_not_a_season_template_answer(world):
    w = world
    citizen, away = w.person("Citizen"), w.person("Away")
    for p in (citizen, away):
        w.fact(p["id"], "nationality", w.country["id"])
    w.stint(citizen, "A")
    stint_out_of_the_league(w, away, "C")
    two, one_away = w.person("Two"), w.person("OneAway")
    w.stint(two, "A", predicate="coached", apps=None, start_year=2009, end_year=2011)
    w.stint(two, "B", predicate="coached", apps=None)
    w.stint(one_away, "A", predicate="coached", apps=None, start_year=2009, end_year=2011)
    stint_out_of_the_league(w, one_away, "C", predicate="coached", apps=None)
    w.run("bundesliga_nationality", "multi_club_coaches")
    nationality = w.only("bundesliga_nationality", country=w.country["wikidata_id"])
    assert set(w.answers(nationality["id"])) == {citizen["id"]}
    [coaches] = w.prompts("multi_club_coaches")
    assert set(w.answers(coaches["id"])) == {two["id"]}


def test_same_template_and_params_in_two_competitions_are_both_stored(world):
    w = world
    p1 = w.person("P1")
    w.fact(w.clubs["A"]["id"], "won", w.comp["id"], start_year=2010, end_year=2011)
    w.stint(p1, "A")
    w.run("club_title_winners")
    [first] = w.prompts("club_title_winners")
    other = w.entity("competition", "Other Test League")
    w.cur.execute(
        "insert into prompts (template, params, text, answer_type, answer_query, competition_id, facts_as_of)"
        " values (%s, %s, 'x', 'person', 'select 1', %s, now())",
        (first["template"], Jsonb(first["params"]), other["id"]))
    w.run("club_title_winners")
    assert w.cur.execute("select count(*) as n from prompts where template = 'club_title_winners'"
                         " and params = %s and competition_id in (%s, %s)",
                         (Jsonb(first["params"]), w.comp["id"], other["id"])).fetchone()["n"] == 2
    with pytest.raises(psycopg.errors.UniqueViolation):
        w.cur.execute(
            "insert into prompts (template, params, text, answer_type, answer_query, competition_id, facts_as_of)"
            " values (%s, %s, 'x', 'person', 'select 1', %s, now())",
            (first["template"], Jsonb(first["params"]), other["id"]))


def test_a_template_filter_deletes_nothing_of_other_templates_or_competitions(world):
    w = world
    p1 = w.person("P1")
    w.stint(p1, "A")
    w.stint(p1, "B")
    w.stint(p1, "A", predicate="coached", apps=None)
    w.run()
    other = w.entity("competition", "Other Test League")
    w.cur.execute(
        "insert into prompts (template, params, text, answer_type, answer_query, competition_id, facts_as_of)"
        " values ('club_pair', '{}', 'x', 'person', 'select 1', %s, now())", (other["id"],))
    coaches = len(w.prompts("club_coaches"))
    assert coaches > 0
    stats = w.run("club_pair")
    assert list(stats) == ["club_pair"] and stats["club_pair"].deleted == 0
    assert len(w.prompts("club_coaches")) == coaches
    assert w.cur.execute("select count(*) as n from prompts where competition_id = %s",
                         (other["id"],)).fetchone()["n"] == 1


def test_unknown_competition_raises(world):
    with pytest.raises(SystemExit, match="No competition entity"):
        generate(world.conn, "Q99999999999", TUNABLES)


def test_competition_without_seasons_raises(world):
    w = world
    empty = w.entity("competition", "Empty Test League")
    with pytest.raises(SystemExit, match="No seasons"):
        generate(w.conn, empty["wikidata_id"], TUNABLES)


def test_a_short_middle_season_cuts_the_relegation_and_promotion_range(conn):
    # 2010 lists only A and B. Relegation event years need seasons e - 1 and e fully listed, so 2010 and 2011
    # are undecidable; promotion event years e = 2010 and 2011 likewise. The latest run is 2012-2013.
    w = World(conn, seasons={2008: "ABC", 2009: "ABD", 2010: "AB", 2011: "ABC", 2012: "ABD", 2013: "ABC"})
    w.run("relegated_clubs", "promoted_clubs")
    for template in ("relegated_clubs", "promoted_clubs"):
        [prompt] = w.prompts(template)
        assert (prompt["params"]["from_year"], prompt["params"]["to_year"]) == (2012, 2013)
        assert set(w.answers(prompt["id"])) == {w.clubs["C"]["id"], w.clubs["D"]["id"]}


def test_an_over_listed_season_is_undecidable(conn):
    w = World(conn, seasons={2008: "ABC", 2009: "ABCD", 2010: "ABC"})
    w.run("relegated_clubs", "promoted_clubs")
    assert w.prompts("relegated_clubs") == [] and w.prompts("promoted_clubs") == []


def test_rejected_prompt_is_left_alone_like_an_approved_one(world):
    w = world
    p1, p2 = w.person("P1"), w.person("P2")
    w.stint(p1, "A")
    w.stint(p1, "B")
    w.run("club_pair")
    prompt = w.prompts("club_pair")[0]
    w.cur.execute("update prompts set status = 'rejected', reviewed_at = now(), review_note = 'no' where id = %s",
                  (prompt["id"],))
    w.stint(p2, "A")
    w.stint(p2, "B")
    rejected_before = w.prompts("club_pair")[0]
    answers_before = w.answers(prompt["id"])

    stats = w.run("club_pair")["club_pair"]

    assert (stats.inserted, stats.updated, stats.deleted, stats.rejected, stats.approved) == (0, 0, 0, 1, 0)
    assert stats.approved_diffs == []
    assert w.prompts("club_pair")[0] == rejected_before  # still rejected, same text, answers and note
    assert w.answers(prompt["id"]) == answers_before
    assert len(w.prompts("club_pair")) == 1  # and not regenerated as a second prompt


def test_rejected_prompt_survives_when_no_longer_produced(world):
    w = world
    p1 = w.person("P1")
    f = w.stint(p1, "B")
    w.stint(p1, "A")
    w.run("club_pair")
    w.cur.execute("update prompts set status = 'rejected', reviewed_at = now(), review_note = 'no'"
                  " where template = 'club_pair' and competition_id = %s", (w.comp["id"],))
    w.cur.execute("delete from facts where id = %s", (f,))
    stats = w.run("club_pair")["club_pair"]
    assert (stats.deleted, stats.rejected_gone) == (0, 1)
    assert len(w.prompts("club_pair")) == 1
