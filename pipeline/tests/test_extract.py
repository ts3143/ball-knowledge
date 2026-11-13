"""Scope rules on synthetic Wikidata-shaped JSON. IDs and names are placeholders, not real facts."""

from datetime import date

import pytest

from ingest.dewiki import CoachStation, PlayerRow
from ingest.extract import (
    Extraction,
    Scope,
    WdTime,
    add_aliases,
    add_entity,
    classify_stint,
    display_name,
    parse_seasons,
    parse_time_string,
    people_facts,
    pick_demonym,
    season_facts,
    strip_disambiguation,
)

SCOPE = Scope(start_year=2000, min_birth_year=1965, languages=("en", "de", "mul"))
CLUB, SQUAD, OTHER_CLUB, LEAGUE = "Q990000001", "Q990000002", "Q990000003", "Q990000004"
_ids = iter(range(1, 10_000))


def time_value(year, precision=9, month=0, day=0):
    return {"snaktype": "value", "datavalue": {"type": "time", "value": {
        "time": f"+{year:04d}-{month:02d}-{day:02d}T00:00:00Z", "precision": precision}}}


def item(qid):
    return {"snaktype": "value", "datavalue": {"type": "wikibase-entityid", "value": {"id": qid}}}


def stmt(value, start=None, end=None, rank="normal", **extra):
    quals = {}
    if start is not None:
        quals["P580"] = [start if isinstance(start, dict) else time_value(start)]
    if end is not None:
        quals["P582"] = [end if isinstance(end, dict) else time_value(end)]
    quals.update(extra)
    return {"id": f"S${next(_ids)}", "rank": rank, "mainsnak": value, "qualifiers": quals}


def person(qid, born=None, female=False, claims=None, labels=None):
    c = {"P31": [stmt(item("Q5"))], "P21": [stmt(item("Q6581072" if female else "Q6581097"))]}
    if born:
        c["P569"] = [stmt(time_value(born))]
    for pid, stmts in (claims or {}).items():
        c[pid] = stmts
    return {"id": qid, "labels": labels or {"en": {"value": f"Test Person {qid}"}}, "claims": c}


def club(qid, coaches=()):
    return {"id": qid, "labels": {"en": {"value": f"Test Club {qid}"}}, "claims": {"P286": list(coaches)}}


def run_people(persons, clubs=None, stations=()):
    ex = Extraction()
    clubs = clubs or {CLUB: club(CLUB), SQUAD: club(SQUAD)}
    in_scope = people_facts({p["id"]: p for p in persons}, clubs, {SQUAD: CLUB}, {CLUB}, SCOPE, ex, list(stations))
    return ex, in_scope


def station(person, start, end, role="head", club_id=CLUB):
    return CoachStation(person=person, club=club_id, start=start, end=end, role=role, page=f"Page {person}",
                        page_id=int(person[1:]), rev_id=7, row=f"{start}–{end or ''} | placeholder")


def coached(ex):
    return [f for f in ex.facts.values() if f.predicate == "coached"]


# --- dates --------------------------------------------------------------------

def test_time_precision():
    assert parse_time_string("+2005-00-00T00:00:00Z", 9) == WdTime(date(2005, 1, 1), 9)
    assert parse_time_string("+2005-07-00T00:00:00Z", 10) == WdTime(date(2005, 7, 1), 10)
    assert parse_time_string("+2005-07-15T00:00:00Z", 11) == WdTime(date(2005, 7, 15), 11)
    assert parse_time_string("+2000-00-00T00:00:00Z", 8) is None  # decade: unknown year


def test_impossible_calendar_date_keeps_year():
    assert parse_time_string("+2005-02-30T00:00:00Z", 11) == WdTime(date(2005, 1, 1), 9)


def test_out_of_range_year_is_unknown_and_reported():
    assert parse_time_string("+20009-00-00T00:00:00Z", 9) is None
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2005, time_value(20009))]})
    ex, in_scope = run_people([p])
    assert in_scope == {"Q1"}  # start 2005 still proves the window; the end is unknown
    (stint,) = [f for f in ex.facts.values() if f.predicate == "played_for"]
    assert (stint.end_year, stint.date_quality) == (None, "open_ended")
    assert [r[3] for r in ex.stats["unreadable_dates"]] == [["P582=+20009-00-00T00:00:00Z"]]


@pytest.mark.parametrize("start, end, born, expected", [
    (1995, 2003, None, ("dated", "in")),
    (1990, 1999, 1975, (None, "ended_before_window")),
    (None, 2001, None, ("dated", "in")),
    (2004, None, None, ("open_ended", "in")),
    (1998, None, 1975, ("open_ended", "in_by_birth_year")),
    (1990, None, 1960, (None, "unproven_dates_born_before_cutoff")),
    (None, None, 1965, ("undated", "in_by_birth_year")),
    (None, None, 1964, (None, "unproven_dates_born_before_cutoff")),
    (None, None, None, (None, "unproven_dates_birth_unknown")),
])
def test_classify_stint(start, end, born, expected):
    t = lambda y: WdTime(date(y, 1, 1), 9) if y else None
    assert classify_stint(t(start), t(end), born, SCOPE) == expected


# --- people -------------------------------------------------------------------

def test_player_stint_in_scope_with_apps():
    p = person("Q1", born=1980, claims={"P54": [
        stmt(item(CLUB), 2001, 2005, P1350=[{"snaktype": "value", "datavalue": {"type": "quantity", "value": {"amount": "+34"}}}]),
        stmt(item(OTHER_CLUB), 2005, 2008),  # not a club in the set: ignored
    ]})
    ex, in_scope = run_people([p])
    assert in_scope == {"Q1"}
    stints = [f for f in ex.facts.values() if f.predicate == "played_for"]
    assert len(stints) == 1
    assert (stints[0].object, stints[0].start_year, stints[0].end_year, stints[0].apps) == (CLUB, 2001, 2005, 34)
    assert stints[0].date_quality == "dated"


def test_deprecated_statement_skipped():
    p = person("Q1", born=1980, claims={"P54": [stmt(item(CLUB), 2001, 2005, rank="deprecated")]})
    ex, in_scope = run_people([p])
    assert in_scope == set()
    assert ex.counts["deprecated_skipped"] == 1


def test_women_on_mens_club_items_excluded():
    p = person("Q1", born=1980, female=True, claims={"P54": [stmt(item(CLUB), 2001, 2005)]})
    ex, in_scope = run_people([p])
    assert in_scope == set()
    assert ex.counts["women_on_mens_club_items"] == 1
    assert not ex.facts


def test_birth_year_rule_for_undated_stints():
    old = person("Q1", born=1950, claims={"P54": [stmt(item(CLUB))]})
    young = person("Q2", born=1990, claims={"P54": [stmt(item(CLUB))]})
    ex, in_scope = run_people([old, young])
    assert in_scope == {"Q2"}
    assert [f.date_quality for f in ex.facts.values() if f.predicate == "played_for"] == ["undated"]
    assert ex.counts["people_dropped_born_before_cutoff"] == 1


def test_squad_item_resolves_to_club():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(SQUAD), 2021, 2023)]})
    ex, _ = run_people([p])
    (stint,) = [f for f in ex.facts.values() if f.predicate == "played_for"]
    assert stint.object == CLUB
    assert stint.qualifiers["via_item"] == SQUAD


def test_attribute_facts_only_for_people_in_scope():
    inside = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2010, 2012)], "P27": [stmt(item("Q990000010"))]})
    outside = person("Q2", born=1950, claims={"P54": [stmt(item(CLUB), 1980, 1985)], "P27": [stmt(item("Q990000010"))]})
    ex, _ = run_people([inside, outside])
    assert {(f.subject, f.predicate) for f in ex.facts.values() if f.predicate != "played_for"} == {
        ("Q1", "born"), ("Q1", "nationality")}


def test_coach_entries_in_both_sources_collapse_to_p286():
    coach = person("Q1", born=1970, claims={"P6087": [stmt(item(CLUB), 2010, 2012)]})
    clubs = {CLUB: club(CLUB, [stmt(item("Q1"), time_value(2010, 11, 7, 1), time_value(2012, 11, 6, 30))]),
             SQUAD: club(SQUAD)}
    ex, _ = run_people([coach], clubs)
    (fact,) = [f for f in ex.facts.values() if f.predicate == "coached"]
    assert fact.qualifiers["source_property"] == "P286"
    assert fact.start_date == date(2010, 7, 1)
    assert len(fact.qualifiers["also_p6087"]) == 1
    assert not ex.stats["coach_source_conflicts"]


def test_overlapping_coach_spells_merge_and_date_disagreement_reported():
    coach = person("Q1", born=1970, claims={"P6087": [stmt(item(CLUB), 2011, 2012)]})
    clubs = {CLUB: club(CLUB, [stmt(item("Q1"), 2010, 2012)]), SQUAD: club(SQUAD)}
    ex, _ = run_people([coach], clubs)
    (fact,) = [f for f in ex.facts.values() if f.predicate == "coached"]
    assert (fact.start_year, fact.qualifiers["source_property"]) == (2010, "P286")
    assert ex.stats["coach_date_disagreements"] == [("Q1", CLUB, [2010, 2012], [2011, 2012])]


def test_undated_p6087_merges_into_dated_p286():
    coach = person("Q1", born=1950, claims={"P6087": [stmt(item(CLUB))]})
    clubs = {CLUB: club(CLUB, [stmt(item("Q1"), 2018, 2019)]), SQUAD: club(SQUAD)}
    ex, _ = run_people([coach], clubs)
    # Born before the cutoff, so the undated P6087 entry is out of scope on its own anyway.
    assert [(f.start_year, f.date_quality) for f in ex.facts.values() if f.predicate == "coached"] == [(2018, "dated")]


def test_separate_coach_spells_both_kept():
    coach = person("Q1", born=1970, claims={"P6087": [stmt(item(CLUB), 2005, 2007)]})
    clubs = {CLUB: club(CLUB, [stmt(item("Q1"), 2015, 2016)]), SQUAD: club(SQUAD)}
    ex, _ = run_people([coach], clubs)
    spells = sorted((f.start_year, f.qualifiers["source_property"]) for f in ex.facts.values() if f.predicate == "coached")
    assert spells == [(2005, "P6087"), (2015, "P286")]
    assert ex.counts["coach_spells_only_in_p6087"] == 1


# --- seasons ------------------------------------------------------------------

def uri(q):
    return f"http://www.wikidata.org/entity/{q}"


def test_seasons_resolve_squad_items_and_winners():
    rows = [
        {"season": uri("Q990000100"), "start": "2020-09-01T00:00:00Z", "end": "2021-05-30T00:00:00Z",
         "team": uri(SQUAD), "parent": uri(CLUB)},
        {"season": uri("Q990000100"), "start": "2020-09-01T00:00:00Z", "end": "2021-05-30T00:00:00Z",
         "team": uri(OTHER_CLUB)},
        {"season": uri("Q990000101"), "start": "2021-08-01T00:00:00Z"},  # no teams listed
    ]
    strip = lambda u: u.rsplit("/", 1)[1]
    seasons, squad_to_club = parse_seasons(rows, strip)
    assert squad_to_club == {SQUAD: CLUB}
    ex = Extraction()
    season_facts(seasons, [{"season": uri("Q990000100"), "winner": uri(SQUAD)}], LEAGUE, squad_to_club, strip, ex)
    competed = {(f.subject, f.start_year) for f in ex.facts.values() if f.predicate == "competed_in"}
    assert competed == {(CLUB, 2020), (OTHER_CLUB, 2020)}
    (won,) = [f for f in ex.facts.values() if f.predicate == "won"]
    assert won.subject == CLUB
    assert [s["teams"] for s in ex.stats["seasons"]] == [2, 0]


# --- aliases ------------------------------------------------------------------

def test_family_name_wins_over_identical_alt_label():
    p = person("Q1", labels={"en": {"value": "Tëst Müllerling"}}, claims={"P734": [stmt(item("Q990000200"))]})
    p["aliases"] = {"en": [{"value": "Müllerling"}]}
    families = {"Q990000200": {"id": "Q990000200", "labels": {"en": {"value": "Müllerling"}}}}
    ex = Extraction()
    add_aliases(ex, "Q1", p, SCOPE, family_names=families)
    assert ex.aliases[("Q1", "mullerling")].kind == "family_name"


def test_entity_named_from_other_language_when_no_preferred_label():
    ex = Extraction()
    add_entity(ex, {"id": "Q1", "labels": {"uk": {"value": "Тест"}}}, "person", SCOPE)
    assert ex.entities["Q1"].canonical_name == "Тест"
    assert ex.stats["no_label"] == [("Q1", "uk", "Тест")]


def test_aliases_with_family_name_and_translit():
    p = person("Q1", labels={"en": {"value": "Tëst Müllerling"}, "de": {"value": "Tëst Müllerling"}},
               claims={"P734": [stmt(item("Q990000200"))]})
    p["aliases"] = {"de": [{"value": "T. Müllerling"}]}
    families = {"Q990000200": {"id": "Q990000200", "labels": {"mul": {"value": "Müllerling"}}}}
    ex = Extraction()
    add_aliases(ex, "Q1", p, SCOPE, family_names=families)
    got = {(a.alias_norm, a.kind) for a in ex.aliases.values()}
    assert got == {
        ("test mullerling", "label"), ("t mullerling", "alt_label"), ("mullerling", "family_name"),
        ("test muellerling", "translit"), ("t muellerling", "translit"), ("muellerling", "translit"),
    }


# --- coach spells from the German Wikipedia infobox ---------------------------------

def test_undated_coach_link_dated_by_infobox_rows():
    link = stmt(item(CLUB))
    p = person("Q1", born=1950, claims={"P6087": [link]})
    ex, in_scope = run_people([p], stations=[station("Q1", 2001, 2004), station("Q1", 1990, 1993)])
    assert in_scope == {"Q1"}
    (f,) = coached(ex)
    assert (f.source, f.start_year, f.end_year, f.date_quality) == ("dewiki", 2001, 2004, "dated")
    assert f.qualifiers["wikidata_links"] == [link["id"]]
    assert f.qualifiers["role"] == "head"
    assert ex.counts["coach_links:dated_by_dewiki"] == 1
    assert ex.counts["coached_p6087:unproven_dates_born_before_cutoff"] == 0


def test_undated_coach_link_dropped_when_infobox_rows_are_before_window():
    p = person("Q1", born=1950, claims={"P6087": [stmt(item(CLUB))]})
    ex, in_scope = run_people([p], stations=[station("Q1", 1985, 1990)])
    assert in_scope == set()
    assert ex.counts["coach_links:dewiki_rows_before_window"] == 1
    assert ex.counts["people_dropped_by_dewiki_rows"] == 1


def test_undated_coach_link_dropped_when_infobox_shows_only_assistant_rows():
    p = person("Q1", born=1975, claims={"P6087": [stmt(item(CLUB))]})
    ex, in_scope = run_people([p], stations=[station("Q1", 2005, 2008, role="other")])
    assert in_scope == set()
    assert ex.counts["coach_links:dewiki_rows_not_head_coach"] == 1


def test_undated_coach_link_without_infobox_rows_keeps_birth_year_rule():
    old = person("Q1", born=1950, claims={"P6087": [stmt(item(CLUB))]})
    young = person("Q2", born=1975, claims={"P6087": [stmt(item(CLUB))]})
    ex, in_scope = run_people([old, young])
    assert in_scope == {"Q2"}
    assert [f.date_quality for f in coached(ex)] == ["undated"]


def test_dated_wikidata_spell_confirmed_by_infobox_keeps_wikidata_years():
    p = person("Q1", born=1960)
    clubs = {CLUB: club(CLUB, [stmt(item("Q1"), 2004, 2007)]), SQUAD: club(SQUAD)}
    ex, _ = run_people([p], clubs, stations=[station("Q1", 2004, 2006)])
    (f,) = coached(ex)
    assert (f.source, f.start_year, f.end_year) == ("wikidata", 2004, 2007)
    assert f.qualifiers["dewiki"][0]["row"] == "2004–2006 | placeholder"
    assert ex.stats["coach_dewiki_disagreements"] == [("Q1", CLUB, [2004, 2007], [2004, 2006], "head")]


def test_second_spell_only_in_infobox_is_added_next_to_wikidata_spell():
    p = person("Q1", born=1960)
    clubs = {CLUB: club(CLUB, [stmt(item("Q1"), 2004, 2007)]), SQUAD: club(SQUAD)}
    ex, _ = run_people([p], clubs, stations=[station("Q1", 2004, 2007), station("Q1", 2012, 2012, "interim")])
    spells = sorted((f.source, f.start_year, f.qualifiers.get("role")) for f in coached(ex))
    assert spells == [("dewiki", 2012, "interim"), ("wikidata", 2004, None)]
    assert ex.stats["coach_dewiki_disagreements"] == []


def test_person_found_only_in_infobox():
    p = person("Q1", born=1970)
    ex, in_scope = run_people([p], stations=[station("Q1", 2010, 2011, "interim"), station("Q1", 2024, None)])
    assert in_scope == {"Q1"}
    spells = sorted((f.start_year, f.end_year, f.date_quality, f.qualifiers["role"]) for f in coached(ex))
    assert spells == [(2010, 2011, "dated", "interim"), (2024, None, "open_ended", "head")]
    assert all("wikidata_links" not in f.qualifiers for f in coached(ex))
    assert ex.counts["people_only_from_dewiki"] == 1
    assert any(f.predicate == "born" for f in ex.facts.values())


def test_infobox_open_ended_row_before_window_is_out():
    p = person("Q1", born=1940)
    _, in_scope = run_people([p], stations=[station("Q1", 1995, None)])
    assert in_scope == set()


def test_wikidata_spell_where_infobox_shows_only_assistant_role_is_dropped_and_reported():
    p = person("Q1", born=1960)
    clubs = {CLUB: club(CLUB, [stmt(item("Q1"), 2004, 2007)]), SQUAD: club(SQUAD)}
    ex, _ = run_people([p], clubs, stations=[station("Q1", 2004, 2007, role="other")])
    assert coached(ex) == []
    assert len(ex.stats["coach_spells_dewiki_says_other_role"]) == 1


def test_woman_found_only_in_infobox_is_excluded():
    p = person("Q1", born=1980, female=True)
    ex, in_scope = run_people([p], stations=[station("Q1", 2015, 2016)])
    assert in_scope == set()
    assert ex.counts["women_on_mens_club_items"] == 1


# --- P39 "position held" on coach statements ----------------------------------

NON_HEAD, CARETAKER = "Q990000010", "Q1050607"


def p39(qid):
    return {"P39": [item(qid)]}


def test_p6087_with_non_head_role_gives_no_fact_and_drops_the_person():
    coach = person("Q1", born=1970, claims={"P6087": [stmt(item(CLUB), 2010, 2012, **p39(NON_HEAD))]})
    ex, in_scope = run_people([coach])
    assert not coached(ex)
    assert "Q1" not in in_scope
    assert not [f for f in ex.facts.values() if f.subject == "Q1"]
    assert ex.counts["coached_p6087:non_head_role"] == 1
    ((who, where, _, roles),) = ex.stats["coach_statements_non_head_role"]
    assert (who, where, roles) == ("Q1", CLUB, [NON_HEAD])


def test_p286_with_non_head_role_gives_no_fact_and_drops_the_person():
    clubs = {CLUB: club(CLUB, [stmt(item("Q1"), 2010, 2012, **p39(NON_HEAD))]), SQUAD: club(SQUAD)}
    ex, in_scope = run_people([person("Q1", born=1970)], clubs)
    assert not coached(ex)
    assert "Q1" not in in_scope
    assert ex.counts["coached_p286:non_head_role"] == 1


def test_caretaker_statement_is_kept_and_marked_interim():
    coach = person("Q1", born=1970, claims={"P6087": [stmt(item(CLUB), 2010, 2012, **p39(CARETAKER))]})
    ex, in_scope = run_people([coach])
    (fact,) = coached(ex)
    assert fact.qualifiers["role"] == "interim"
    assert "Q1" in in_scope
    assert not ex.stats["coach_statements_non_head_role"]


def test_coach_statement_without_p39_is_kept():
    clubs = {CLUB: club(CLUB, [stmt(item("Q1"), 2010, 2012)]), SQUAD: club(SQUAD)}
    ex, _ = run_people([person("Q1", born=1970)], clubs)
    (fact,) = coached(ex)
    assert "role" not in fact.qualifiers


def test_mixed_p39_values_are_rejected_unless_all_are_head_roles():
    coach = person("Q1", born=1970, claims={"P6087": [stmt(item(CLUB), 2010, 2012, P39=[item(CARETAKER), item(NON_HEAD)])]})
    ex, _ = run_people([coach])
    assert not coached(ex)


def test_rejected_p6087_does_not_collapse_into_or_confirm_anything():
    coach = person("Q1", born=1970, claims={"P6087": [stmt(item(CLUB), 2010, 2012, **p39(NON_HEAD))]})
    clubs = {CLUB: club(CLUB, [stmt(item("Q1"), 2010, 2012)]), SQUAD: club(SQUAD)}
    ex, _ = run_people([coach], clubs)
    (fact,) = coached(ex)
    assert fact.qualifiers["source_property"] == "P286"
    assert "also_p6087" not in fact.qualifiers
    assert not ex.counts["coach_entries_collapsed"]


def test_rejected_statement_does_not_confirm_or_absorb_an_infobox_row():
    coach = person("Q1", born=1970, claims={"P6087": [stmt(item(CLUB), 2010, 2012, **p39(NON_HEAD))]})
    ex, _ = run_people([coach], stations=[station("Q1", 2010, 2012)])
    (fact,) = coached(ex)
    assert fact.source == "dewiki"
    assert "wikidata_links" not in fact.qualifiers
    assert "dewiki" not in fact.qualifiers


# --- player stints from the German Wikipedia career tables --------------------------

def row(person, start, end, role="first_team", apps=None, goals=None, club_id=CLUB):
    return PlayerRow(person=person, club=club_id, start=start, end=end, role=role, apps=apps, goals=goals,
                     page=f"Page {person}", page_id=int(person[1:]), rev_id=7,
                     row=f"{start}–{end or ''} | placeholder")


def quantity(n):
    return {"snaktype": "value", "datavalue": {"type": "quantity", "value": {"amount": f"+{n}"}}}


def played(ex):
    return sorted((f for f in ex.facts.values() if f.predicate == "played_for"),
                  key=lambda f: (f.start_year or 0, f.source))


def run_rows(persons, rows):
    ex = Extraction()
    clubs = {CLUB: club(CLUB), SQUAD: club(SQUAD)}
    in_scope = people_facts({p["id"]: p for p in persons}, clubs, {SQUAD: CLUB}, {CLUB}, SCOPE, ex, [], list(rows))
    return ex, in_scope


def test_rule1_undated_link_replaced_by_senior_rows_in_the_window():
    link = stmt(item(CLUB))
    p = person("Q1", born=1950, claims={"P54": [link]})        # born before the cutoff: dropped without rows
    ex, in_scope = run_rows([p], [row("Q1", 2001, 2004, apps=30, goals=2), row("Q1", 1990, 1993),
                                  row("Q1", 2005, 2005, role="loan", apps=4, goals=0),
                                  row("Q1", 2001, 2002, role="reserve"), row("Q1", 1995, 2000, role="youth")])
    assert in_scope == {"Q1"}
    got = played(ex)
    assert [(f.source, f.start_year, f.end_year, f.apps, f.goals, f.date_quality) for f in got] == [
        ("dewiki", 2001, 2004, 30, 2, "dated"), ("dewiki", 2005, 2005, 4, 0, "dated")]
    assert all(f.qualifiers["wikidata_links"] == [link["id"]] for f in got)
    assert got[1].qualifiers["role"] == "loan"
    assert ex.counts["players_rule1_rows_replacing_unproven_link"] == 2
    assert ex.counts["players_rule1_links_replaced"] == 1


def test_rule1_person_enters_scope_and_unproven_link_without_rows_still_dropped():
    old = person("Q1", born=1950, claims={"P54": [stmt(item(CLUB))]})
    other = person("Q2", born=1950, claims={"P54": [stmt(item(CLUB))]})
    ex, in_scope = run_rows([old, other], [row("Q1", 2001, 2004)])
    assert in_scope == {"Q1"}                                  # Q2 has no rows: birth-year rule as before
    assert ex.counts["played_for:unproven_dates_born_before_cutoff"] == 1


def test_person_leaves_scope_when_all_evidence_is_youth_or_reserve():
    young = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB))]})    # kept by birth year without rows
    assert run_rows([young], [])[1] == {"Q1"}
    ex, in_scope = run_rows([young], [row("Q1", 2005, 2007, role="reserve"), row("Q1", 2003, 2005, role="youth")])
    assert in_scope == set()
    assert ex.counts["players_unproven_links_dropped:reserve_youth_rows_only"] == 1
    assert ex.counts["people_dropped_by_dewiki_rows"] == 1


def test_unproven_link_dropped_when_senior_rows_are_before_the_window():
    p = person("Q1", born=1950, claims={"P54": [stmt(item(CLUB))]})
    ex, in_scope = run_rows([p], [row("Q1", 1985, 1990)])
    assert in_scope == set()
    assert ex.counts["players_unproven_links_dropped:senior_rows_before_window"] == 1


def test_rule2_end_year_filled_from_row_with_same_or_adjacent_start():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2005), stmt(item(CLUB), 2012)]})
    ex, _ = run_rows([p], [row("Q1", 2006, 2009, apps=50), row("Q1", 2012, None, apps=9)])
    first, second = played(ex)
    assert (first.source, first.start_year, first.end_year, first.date_quality) == ("wikidata", 2005, 2009, "dated")
    assert first.qualifiers["dewiki_filled"]["end_year"] == 2009
    assert first.qualifiers["dewiki_filled"]["row"] == "2006–2009 | placeholder"
    assert (second.end_year, second.date_quality) == (None, "open_ended")   # open row confirms the current spell
    assert ex.counts["players_rule2_end_year_filled"] == 1
    assert ex.counts["players_rule2_open_spell_confirmed_current"] == 1
    assert second.qualifiers["dewiki_current"] is True and "dewiki_current" not in first.qualifiers
    assert len(played(ex)) == 2                                              # no extra dewiki facts


def test_rule2_not_applied_when_start_years_are_two_apart():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2005)]})
    ex, _ = run_rows([p], [row("Q1", 2007, 2009)])
    assert [(f.end_year, f.date_quality) for f in played(ex) if f.source == "wikidata"] == [(None, "open_ended")]
    assert ex.counts["players_rule2_end_year_filled"] == 0


def test_rule2_unproven_open_spell_dated_into_the_window_or_out_of_it():
    both = person("Q1", born=1950, claims={"P54": [stmt(item(CLUB), 1998), stmt(item(OTHER_CLUB), 1990)]})
    ex, in_scope = run_rows([both], [row("Q1", 1998, 2003)])
    assert [(f.source, f.start_year, f.end_year, f.date_quality) for f in played(ex)] == [("wikidata", 1998, 2003, "dated")]
    assert ex.counts["players_unproven_link_proven_by_dewiki_row"] == 1
    ex, in_scope = run_rows([both], [row("Q1", 1998, 1999)])
    assert in_scope == set()
    assert ex.counts["players_unproven_link_ended_before_window"] == 1


def test_rule3_apps_filled_only_when_wikidata_has_none():
    a = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2001, 2004)]})
    b = person("Q2", born=1990, claims={"P54": [stmt(item(CLUB), 2001, 2004, P1350=[quantity(40)])]})
    ex, _ = run_rows([a, b], [row("Q1", 2001, 2004, apps=30, goals=3), row("Q2", 2001, 2004, apps=99, goals=9)])
    fa, fb = (next(f for f in played(ex) if f.subject == q) for q in ("Q1", "Q2"))
    assert (fa.apps, fa.goals, fa.qualifiers["dewiki_apps_filled"]["apps"]) == (30, 3, 30)
    assert (fb.apps, fb.goals) == (40, None) and "dewiki_apps_filled" not in fb.qualifiers
    assert ex.counts["players_rule3_apps_filled"] == 1


def test_rule3_skipped_when_several_rows_overlap():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2001, 2008)]})
    ex, _ = run_rows([p], [row("Q1", 2001, 2004, apps=30), row("Q1", 2005, 2008, apps=20)])
    (f,) = played(ex)
    assert f.apps is None
    assert ex.counts["players_rule3_skipped_several_rows"] == 1


def test_rule4_stint_dropped_when_only_reserve_or_youth_rows_overlap():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2005, 2008), stmt(item(OTHER_CLUB), 2005, 2008)]})
    ex, in_scope = run_rows([p], [row("Q1", 2005, 2007, role="reserve"), row("Q1", 2003, 2005, role="youth")])
    assert in_scope == set()
    assert ex.counts["players_rule4_stint_dropped_reserve_youth_only"] == 1
    assert ex.stats["players_rule4_stint_dropped_reserve_youth_only"][0][5] == [
        "2005–2007 | placeholder", "2003–2005 | placeholder"]


def test_rule4_does_not_apply_when_a_senior_row_overlaps_too():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2005, 2008)]})
    ex, _ = run_rows([p], [row("Q1", 2005, 2007, role="reserve"), row("Q1", 2007, 2008)])
    assert [(f.start_year, f.end_year) for f in played(ex)] == [(2005, 2008)]


def test_rule5_senior_row_nobody_overlaps_becomes_a_fact():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2001, 2004)]})
    ex, _ = run_rows([p], [row("Q1", 2001, 2004), row("Q1", 2010, 2012, apps=7)])
    old, new = played(ex)
    assert old.source == "wikidata"
    assert (new.source, new.start_year, new.end_year, new.apps) == ("dewiki", 2010, 2012, 7)
    assert "wikidata_links" not in new.qualifiers
    assert ex.counts["players_rule5_rows_only_source"] == 1


def test_rule5_not_applied_where_wikidata_has_a_stint_that_ended_before_the_window():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 1995, 1999)]})
    ex, _ = run_rows([p], [row("Q1", 1998, 2001)])
    assert played(ex) == []                      # Wikidata says it ended in 1999: no dewiki fact for 1998–2001
    assert ex.counts["players_rule5_blocked_by_wikidata_stint_before_window"] == 1


def test_rule6_different_years_keep_wikidata_and_report():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2001, 2005)]})
    ex, _ = run_rows([p], [row("Q1", 2002, 2006)])
    (f,) = played(ex)
    assert (f.start_year, f.end_year) == (2001, 2005)
    assert f.qualifiers["dewiki"][0]["row"] == "2002–2006 | placeholder"
    assert ex.counts["players_rule6_years_disagree"] == 1


def test_rule7_no_rows_at_the_club_leaves_the_stint_alone():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2001, 2005)]})
    ex, _ = run_rows([p], [])
    (f,) = played(ex)
    assert f.qualifiers.keys() >= {"P580"} and "dewiki" not in f.qualifiers and f.apps is None
    assert ex.counts["players_rule7_unchanged_no_dewiki_rows"] == 1
    assert ex.counts["players_rule2_end_year_filled"] == 0


def test_coach_end_year_filled_from_infobox_row():
    clubs = {CLUB: club(CLUB, [stmt(item("Q1"), 2004)]), SQUAD: club(SQUAD)}
    ex, _ = run_people([person("Q1", born=1960)], clubs, stations=[station("Q1", 2004, 2007), station("Q1", 2012, 2013)])
    spells = sorted(coached(ex), key=lambda f: f.start_year)
    assert [(f.source, f.start_year, f.end_year, f.date_quality) for f in spells] == [
        ("wikidata", 2004, 2007, "dated"), ("dewiki", 2012, 2013, "dated")]
    assert spells[0].qualifiers["dewiki_filled"]["end_year"] == 2007
    assert ex.counts["coach_end_year_filled"] == 1


def test_rule4_keeps_the_stint_when_a_senior_row_at_the_club_was_unreadable():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2005, 2008)]})
    ex = Extraction()
    clubs = {CLUB: club(CLUB), SQUAD: club(SQUAD)}
    rows = [row("Q1", 2005, 2007, role="reserve")]
    in_scope = people_facts({"Q1": p}, clubs, {SQUAD: CLUB}, {CLUB}, SCOPE, ex, [], rows, {("Q1", CLUB)})
    assert in_scope == {"Q1"}
    assert [(f.source, f.start_year, f.end_year) for f in played(ex)] == [("wikidata", 2005, 2008)]
    assert ex.counts["players_rule4_kept_unreadable_senior_row"] == 1
    assert ex.counts["players_rule4_stint_dropped_reserve_youth_only"] == 0


def test_unproven_link_kept_by_the_old_rule_when_a_senior_row_was_unreadable():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB))]})
    ex = Extraction()
    clubs = {CLUB: club(CLUB), SQUAD: club(SQUAD)}
    in_scope = people_facts({"Q1": p}, clubs, {SQUAD: CLUB}, {CLUB}, SCOPE, ex, [],
                            [row("Q1", 2005, 2007, role="reserve")], {("Q1", CLUB)})
    assert in_scope == {"Q1"}
    assert ex.counts["players_unproven_link_unreadable_row_guard:kept_old_rule"] == 1


def test_rule2_open_current_row_is_not_closed_by_an_earlier_loan_row():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2022)]})
    ex, _ = run_rows([p], [row("Q1", 2021, 2022, role="loan", apps=4), row("Q1", 2022, None, apps=60)])
    (f,) = played(ex)
    assert (f.source, f.start_year, f.end_year, f.date_quality) == ("wikidata", 2022, None, "open_ended")
    assert "dewiki_filled" not in f.qualifiers
    assert ex.counts["players_rule2_end_year_filled"] == 0
    assert ex.counts["players_rule2_open_spell_confirmed_current"] == 1
    assert f.qualifiers["dewiki_current"] is True


def test_rule2_never_ends_a_spell_before_it_starts():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2005)]})
    ex, _ = run_rows([p], [row("Q1", 2004, 2004)])
    wd = [f for f in played(ex) if f.source == "wikidata"]
    assert [(f.start_year, f.end_year) for f in wd] == [(2005, None)]
    assert "dewiki_filled" not in wd[0].qualifiers
    assert all(f.end_year is None or f.end_year >= f.start_year for f in played(ex))


def test_coach_open_head_row_is_not_closed_by_an_interim_row_of_the_same_year():
    clubs = {CLUB: club(CLUB, [stmt(item("Q1"), 2023)]), SQUAD: club(SQUAD)}
    ex, _ = run_people([person("Q1", born=1970)], clubs,
                       stations=[station("Q1", 2023, 2023, role="interim"), station("Q1", 2023, None)])
    (f,) = coached(ex)
    assert (f.source, f.start_year, f.end_year, f.date_quality) == ("wikidata", 2023, None, "open_ended")
    assert "dewiki_filled" not in f.qualifiers
    assert ex.counts["coach_end_year_filled"] == 0
    assert f.qualifiers["dewiki_current"] is True


def test_unreadable_senior_row_guard_keeps_a_stint_that_only_a_youth_row_overlaps():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2023)]})
    ex = Extraction()
    clubs = {CLUB: club(CLUB), SQUAD: club(SQUAD)}
    in_scope = people_facts({"Q1": p}, clubs, {SQUAD: CLUB}, {CLUB}, SCOPE, ex, [],
                            [row("Q1", 2018, 2023, role="youth")], {("Q1", CLUB)})
    assert in_scope == {"Q1"}
    assert [(f.source, f.start_year, f.end_year) for f in played(ex)] == [("wikidata", 2023, None)]
    assert ex.counts["players_rule4_kept_unreadable_senior_row"] == 1


def test_guard_label_for_a_link_the_old_rule_drops():
    p = person("Q1", born=1950, claims={"P54": [stmt(item(CLUB))]})
    ex = Extraction()
    clubs = {CLUB: club(CLUB), SQUAD: club(SQUAD)}
    in_scope = people_facts({"Q1": p}, clubs, {SQUAD: CLUB}, {CLUB}, SCOPE, ex, [],
                            [row("Q1", 2005, 2007, role="reserve")], {("Q1", CLUB)})
    assert in_scope == set()
    assert ex.counts["players_unproven_link_unreadable_row_guard:dropped_old_rule"] == 1
    assert ex.counts["players_unproven_link_unreadable_row_guard:kept_old_rule"] == 0


def test_drop_label_when_senior_rows_in_the_window_are_covered_by_a_wikidata_stint():
    p = person("Q1", born=1950, claims={"P54": [stmt(item(CLUB)), stmt(item(CLUB), 2001, 2005)]})
    ex, _ = run_rows([p], [row("Q1", 2002, 2003, apps=5)])
    assert ex.counts["players_unproven_links_dropped:senior_rows_covered_by_wikidata_stint"] == 1
    assert ex.counts["players_unproven_links_dropped:senior_rows_before_window"] == 0


def test_dewiki_player_source_ref_survives_closing_a_current_row():
    p = person("Q1", born=1990, claims={"P54": [stmt(item(OTHER_CLUB), 2001, 2002)]})
    refs = []
    for end in (None, 2026):
        ex, _ = run_rows([p], [row("Q1", 2022, end, apps=3)])
        refs.append([(f.source_ref, f.end_year) for f in played(ex) if f.source == "dewiki"])
    assert refs[0][0][0] == refs[1][0][0]
    assert (refs[0][0][1], refs[1][0][1]) == (None, 2026)


def test_dewiki_coach_source_ref_survives_closing_a_spell():
    refs = []
    for end in (None, 2026):
        ex, _ = run_people([person("Q1", born=1970)], stations=[station("Q1", 2022, end)])
        refs.append([f.source_ref for f in coached(ex) if f.source == "dewiki"])
    assert refs[0] == refs[1] and len(refs[0]) == 1


def test_loan_and_permanent_row_of_the_same_start_year_get_distinct_stable_refs():
    p = person("Q1", born=1990)
    ex, _ = run_rows([p], [row("Q1", 2019, 2019, role="loan"), row("Q1", 2019, None)])
    refs = {f.source_ref: f.end_year for f in ex.facts.values() if f.source == "dewiki"}
    assert len(refs) == 2 and sorted(refs.values(), key=lambda e: e or 9999) == [2019, None]
    loan_ref = next(r for r, e in refs.items() if e == 2019)
    assert not loan_ref.endswith("+")


def test_people_facts_is_deterministic_on_the_same_input():
    import copy
    persons = [person("Q1", born=1990, claims={"P54": [stmt(item(CLUB), 2005), stmt(item(CLUB))]}),
               person("Q2", born=1950, claims={"P54": [stmt(item(CLUB))]})]
    rows = [row("Q1", 2005, 2009, apps=10), row("Q1", 2012, 2013), row("Q2", 2001, 2003, apps=7)]
    first, _ = run_rows(copy.deepcopy(persons), rows)
    second, _ = run_rows(copy.deepcopy(persons), rows)
    assert {r: f.__dict__ for r, f in first.facts.items()} == {r: f.__dict__ for r, f in second.facts.items()}


# --- display names, demonyms and the enwiki_title alias ---------------------------------

@pytest.mark.parametrize("title, expected", [
    ("Placeholder Name (footballer, born 1973)", "Placeholder Name"),
    ("Placeholder Name", "Placeholder Name"),
    ("Placeholder Name (footballer)", "Placeholder Name"),
    ("1. FC Placeholder (men) (football)", "1. FC Placeholder (men)"),  # only the trailing one
    ("Åilton Test (A) ", "Åilton Test"),
    ("(Only parenthetical)", None),
    ("", None),
    (None, None),
])
def test_strip_disambiguation(title, expected):
    assert strip_disambiguation(title) == expected


def test_display_name_falls_back_to_the_label():
    assert display_name("Placeholder Name (footballer)", "Label Name") == "Placeholder Name"
    assert display_name(None, "Label Name") == "Label Name"
    assert display_name("(x)", "Label Name") == "Label Name"


def demonym(text, lang="en", rank="normal"):
    return {"mainsnak": {"snaktype": "value", "datavalue": {"type": "monolingualtext",
                                                               "value": {"text": text, "language": lang}}},
            "rank": rank}


def country(*claims):
    return {"id": "Q1", "claims": {"P1549": list(claims)}}


def test_demonym_prefers_preferred_rank_then_english_then_statement_order():
    assert pick_demonym(country(demonym("Firstish"), demonym("Secondish"))) == "Firstish"
    assert pick_demonym(country(demonym("Firstish"), demonym("Secondish", rank="preferred"))) == "Secondish"
    assert pick_demonym(country(demonym("Deutsch", "de", "preferred"), demonym("Firstish"))) == "Firstish"
    assert pick_demonym(country(demonym("Britishish", "en-gb"), demonym("Plainish"))) == "Plainish"
    assert pick_demonym(country(demonym("Britishish", "en-gb"))) == "Britishish"
    assert pick_demonym(country(demonym("Gone", rank="deprecated"), demonym("Kept"))) == "Kept"


def test_no_english_demonym():
    assert pick_demonym(country(demonym("Deutsch", "de"))) is None
    assert pick_demonym({"id": "Q1"}) is None
    assert pick_demonym(country()) is None
    assert pick_demonym(country({"mainsnak": {"snaktype": "novalue"}, "rank": "normal"})) is None


def test_entities_get_display_name_and_countries_a_demonym():
    ex = Extraction()
    add_entity(ex, {"id": "Q1", "labels": {"en": {"value": "Label Name"}},
                    "sitelinks": {"enwiki": {"title": "Better Name (athlete)"}}}, "person", SCOPE)
    add_entity(ex, {"id": "Q2", "labels": {"en": {"value": "Testland"}}, "claims": {"P1549": [demonym("Testish")]}},
               "country", SCOPE)
    add_entity(ex, {"id": "Q3", "labels": {"en": {"value": "Elsewhere"}}, "claims": {"P1549": [demonym("Nope")]}},
               "club", SCOPE)  # only countries carry a demonym
    assert (ex.entities["Q1"].display_name, ex.entities["Q1"].demonym) == ("Better Name", None)
    assert (ex.entities["Q2"].display_name, ex.entities["Q2"].demonym) == ("Testland", "Testish")
    assert (ex.entities["Q3"].display_name, ex.entities["Q3"].demonym) == ("Elsewhere", None)


def test_enwiki_title_alias_ranks_after_label_and_before_family_name():
    p = person("Q1", labels={"en": {"value": "Tëst Müllerling"}}, claims={"P734": [stmt(item("Q990000200"))]})
    p["sitelinks"] = {"enwiki": {"title": "Test Mullerling (footballer)"}}
    p["aliases"] = {"en": [{"value": "Test Müllerling"}]}
    families = {"Q990000200": {"id": "Q990000200", "labels": {"en": {"value": "Mullerling"}}}}
    ex = Extraction()
    add_aliases(ex, "Q1", p, SCOPE, family_names=families)
    kinds = {a.alias_norm: a.kind for a in ex.aliases.values()}
    # "test mullerling" is the label's norm too: the label keeps it; the alt label is the same text, too
    assert kinds["test mullerling"] == "label" and kinds["mullerling"] == "family_name"
    # a title unlike the label gets its own alias
    q = person("Q2", labels={"en": {"value": "Ailtón Gonçalves da Silva"}})
    q["sitelinks"] = {"enwiki": {"title": "Ailton (footballer, born 1973)"}}
    add_aliases(ex, "Q2", q, SCOPE)
    assert (ex.aliases[("Q2", "ailton")].kind, ex.aliases[("Q2", "ailton")].alias) == ("enwiki_title", "Ailton")
    # an item that only lends its names to another entity (a squad) does not add its title
    add_aliases(ex, "Q3", q, SCOPE, own_title=False)
    assert ("Q3", "ailton") not in ex.aliases


@pytest.mark.db
def test_the_database_accepts_the_enwiki_title_alias_kind(conn):
    from tests.db_world import World
    w = World(conn)
    person_ = w.person("Placeholder Person")
    conn.execute("insert into entity_aliases (entity_id, alias, alias_norm, kind, lang) values"
                 " (%s, 'Placeholder', 'placeholder', 'enwiki_title', 'en')", (person_["id"],))
    conn.execute("update entities set display_name = 'Shown', demonym = 'Testish' where id = %s", (person_["id"],))
    with pytest.raises(Exception, match="entity_aliases_kind_check"), conn.transaction():
        conn.execute("insert into entity_aliases (entity_id, alias, alias_norm, kind) values (%s, 'X', 'x', 'bogus')",
                     (person_["id"],))
