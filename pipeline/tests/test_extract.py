"""Scope rules on synthetic Wikidata-shaped JSON. IDs and names are placeholders, not real facts."""

from datetime import date

import pytest

from ingest.dewiki import CoachStation
from ingest.extract import (
    Extraction,
    Scope,
    WdTime,
    add_aliases,
    add_entity,
    classify_stint,
    parse_seasons,
    parse_time_string,
    people_facts,
    season_facts,
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
