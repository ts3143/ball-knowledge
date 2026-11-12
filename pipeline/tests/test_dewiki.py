"""Infobox coaching-table parsing on synthetic wikitext. Club and page names are placeholders."""

from collections import Counter

import pytest

from ingest.dewiki import (
    Page,
    coach_stations,
    match_club,
    parse_apps,
    parse_stations,
    parse_years,
    player_rows,
    station_role,
)

CLUB = "Q990000001"
CLUB_TITLES = {
    "Example FC": (CLUB, None),
    "Example": (CLUB, None),                       # redirect to the article
    "Example FC II": (CLUB, "Second team"),        # redirect to a section
}


def infobox(*rows: str, extra: str = "") -> str:
    return ("{{Infobox Fußballspieler\n| kurzname = Placeholder\n| vereine_tabelle =\n"
            "{{Team-Station|1980–1990|[[Example FC]]|100 (5)}}\n| trainer_tabelle =\n"
            + "\n".join(rows) + f"\n{extra}}}}}\n'''Placeholder''' is a placeholder.")


@pytest.mark.parametrize("cell, expected", [
    ("2001–2004<ref>Placeholder 2001/02</ref>", (2001, 2004)),
    ("2006–01/07", None),
    ("2006–07", None),
    ("2022–2021", None),
    ("1999–2013", (1999, 2013)),
    ("2016", (2016, 2016)),
    ("2016     ", (2016, 2016)),
    ("seit 2023", (2023, None)),
    ("2023–", (2023, None)),
    ("07/2019–06/2021", (2019, 2021)),
    ("{{0|0000}}–1972", None),
    ("–2000", None),
    ("", None),
    ("19??–19??", None),
    ("10/81–02/83", None),
    ("2003–2004, 2006", None),
])
def test_parse_years(cell, expected):
    assert parse_years(cell) == expected


def test_parse_stations_reads_only_the_coaching_table():
    text = infobox("{{Team-Station|2001–2004|[[Example FC]]}}",
                   "{{Team-Station|2005|→ Example FC (interim)}}")
    rows = parse_stations(text)
    assert [(r.start, r.end, r.target, r.name, r.last) for r in rows] == [
        (2001, 2004, "Example FC", "Example FC", False),
        (2005, 2005, None, "Example FC", True),
    ]


def test_no_coaching_table():
    assert parse_stations("{{Infobox Fußballspieler\n| kurzname = X\n}}") is None
    assert parse_stations("No infobox at all.") is None


def test_nested_templates_and_piped_links():
    (row,) = parse_stations(infobox("{{Team-Station|{{0|0000}}2008–2009|[[Example FC|Example]] {{0}}}}"))
    assert (row.start, row.end, row.target, row.name) == (2008, 2009, "Example FC", "Example")


@pytest.mark.parametrize("team, role", [
    ("[[Example FC]]", "head"),
    ("[[Example FC]] (interim)", "interim"),
    ("[[Example FC]] (Interimstrainer)", "interim"),
    ("[[Example FC]] (Co-Trainer)", "other"),
    ("[[Example FC]] (Konditionstrainer)", "other"),
    ("[[Example FC]] U-19", "other"),
    ("Example FC (U23)", "other"),
    ("[[Example FC]] (Jugend)", "other"),
    ("[[Example FC#Second team|Example FC Amateure]]", "other"),
    ("[[Example FC II]]", "other"),                # redirect to a section
    ("Example FC II (Co-Trainer; interim)", "other"),
    ("[[Example FC]] (Spielertrainer)", "head"),
    ("[[Example FC]]<ref>Placeholder source.</ref>", "head"),
    ("[[Example FC]] (Verbindungstrainer)", "other"),
    ("[[Example FC]] (Co- und Interimstrainer)", "other"),
    ("[[Example FC]] (TW-Trainer)", "other"),
    ("[[Example FC]] Am.", "other"),
    ("[[Example FC]] IV", "other"),
    ("Example FC (Trainerstab)", "other"),
])
def test_station_roles(team, role):
    (row,) = parse_stations(infobox(f"{{{{Team-Station|2001|{team}}}}}"))
    club, section = match_club(row, CLUB_TITLES)
    assert club == CLUB
    assert station_role(row, section) == role


def test_unlinked_team_name_that_is_not_a_club_title_is_ignored():
    (row,) = parse_stations(infobox("{{Team-Station|2001|Example FC U23}}"))
    assert match_club(row, CLUB_TITLES) is None


def test_linked_row_matches_on_target_not_display_text():
    (row,) = parse_stations(infobox("{{Team-Station|2001|[[Example FC (basketball)|Example FC]]}}"))
    assert match_club(row, CLUB_TITLES) is None


def test_lowercase_and_underscored_link_targets_match():
    (row,) = parse_stations(infobox("{{Team-Station|2001|[[example_FC]]}}"))
    assert match_club(row, CLUB_TITLES) == (CLUB, False)


def test_coach_stations_reports_unusable_rows():
    text = infobox("{{Team-Station|1995–|[[Example FC]]}}",       # open-ended, but not the last row
                   "{{Team-Station||[[Example FC]]}}",            # no years
                   "{{Team-Station|2001–2003|[[Elsewhere SC]]}}",  # not our club: ignored
                   "{{Team-Station|seit 2024|[[Example FC]]}}")
    pages = {"Placeholder": Page("Placeholder", 1, 10, text), "Unlinked page": Page("Unlinked page", 2, 20, text)}
    counts, problems = Counter(), []
    stations = coach_stations(pages, {"Placeholder": "Q990000100"}, CLUB_TITLES, counts, problems)
    assert [(s.person, s.start, s.end, s.role, s.page_id, s.rev_id) for s in stations] == [
        ("Q990000100", 2024, None, "head", 1, 10)]
    assert sorted(r for *_, r in problems) == [
        "open-ended but not the current job", "open-ended but not the current job",
        "page has no Wikidata item", "years unreadable", "years unreadable"]
    assert counts["dewiki_rows_at_our_clubs:head"] == 6


def test_multi_year_interim_row_is_not_used():
    text = infobox("{{Team-Station|2016–2025|[[Example FC]] (Co-Trainer)}}",
                   "{{Team-Station|2019–2025|→ Example FC (interim)}}",
                   "{{Team-Station|2026|→ Example FC (interim)}}")
    counts, problems = Counter(), []
    stations = coach_stations({"P": Page("P", 1, 1, text)}, {"P": "Q990000100"}, CLUB_TITLES, counts, problems)
    assert [(s.start, s.end, s.role) for s in stations] == [(2016, 2025, "other"), (2026, 2026, "interim")]
    assert [r for *_, r in problems] == ["interim row spanning several years"]


# --- player career tables -------------------------------------------------------

PLAYER_TITLES = {**CLUB_TITLES, "Example FC Amateure": (CLUB, "Second team")}


def career(*rows: str, youth: tuple[str, ...] = ()) -> str:
    return ("{{Infobox Fußballspieler\n| kurzname = Placeholder\n| jugendvereine_tabelle =\n"
            + "\n".join(youth) + "\n| vereine_tabelle =\n" + "\n".join(rows) + "\n}}\nPlaceholder.")


def parsed(*rows: str, youth: tuple[str, ...] = (), titles=None):
    counts, problems = Counter(), []
    out = player_rows({"P": Page("P", 1, 10, career(*rows, youth=youth))}, {"P": "Q990000100"},
                      titles or PLAYER_TITLES, counts, problems)
    return out, counts, problems


@pytest.mark.parametrize("cell, expected", [
    ("215 (126)", (215, 126)),
    ("68 (42)", (68, 42)),
    ("34 (3)", (34, 3)),
    ("12", (12, None)),
    ("", (None, None)),
    ("? (?)", (None, None)),
])
def test_parse_apps(cell, expected):
    assert parse_apps(cell) == expected


def test_apps_cells_with_zero_padding_templates():
    rows, _, _ = parsed("{{Team-Station|2004–2005|[[Example FC]]|34 {{0}}(3)}}",
                        "{{Team-Station|2006–2007|[[Example FC]]|1 {{0|00}}(0)}}",
                        "{{Team-Station|2008–2009|[[Example FC]]|}}",
                        "{{Team-Station|2010–2011|[[Example FC]]}}")
    assert [(r.apps, r.goals) for r in rows] == [(34, 3), (1, 0), (None, None), (None, None)]


def test_loan_by_arrow_and_by_note_counts_as_senior_loan():
    rows, _, _ = parsed("{{Team-Station|2009|→ [[Example FC]] (Leihe)|2 {{0}}(0)}}",
                        "{{Team-Station|2010|→ [[Example FC]]|1 (0)}}",
                        "{{Team-Station|2011|[[Example FC]] (Leihe)|1 (0)}}")
    assert [(r.start, r.role) for r in rows] == [(2009, "loan"), (2010, "loan"), (2011, "loan")]


def test_loan_by_named_argument_leihe_and_apps_cell_is_still_found():
    rows, counts, _ = parsed("{{Team-Station|2009|[[Example FC]]|12 {{0}}(1)|leihe=1}}",
                             "{{Team-Station|2010|[[Example FC]]|leihe=ja}}",
                             "{{Team-Station|2011|[[Example FC]]|5 (0)|leihe=}}")
    assert [(r.role, r.apps) for r in rows] == [("loan", 12), ("loan", None), ("first_team", 5)]
    assert counts["dewiki_player_apps_cells_unreadable"] == 0


def test_reserve_by_section_anchor_redirect_suffix_and_display_name():
    rows, counts, _ = parsed(
        "{{Team-Station|2001–2002|[[Example FC#Second team|Example FC II]]|3 (0)}}",   # anchor
        "{{Team-Station|2002–2003|[[Example FC Amateure]]|3 (0)}}",                     # redirect to a section
        "{{Team-Station|2003–2004|[[Example FC II]]|3 (0)}}",                           # redirect to a section
        "{{Team-Station|2004–2005|[[Example FC U23]]|3 (0)}}",                          # own article, suffix
        "{{Team-Station|2005–2006|Example FC II|3 (0)}}",                               # unlinked
        "{{Team-Station|2006–2007|[[Example FC#History|Example FC Amateure]]}}",
        "{{Team-Station|2007–2008|[[Elsewhere SC II]]}}")                               # not ours
    assert [r.role for r in rows] == ["reserve"] * 6
    assert counts["dewiki_player_rows_at_our_clubs:reserve"] == 6


def test_youth_table_rows_and_youth_teams_in_the_main_table():
    rows, _, _ = parsed("{{Team-Station|2001–2002|Example FC U19}}", "{{Team-Station|2003–2004|[[Example FC]]}}",
                        youth=("{{Team-Station|1996–2001|[[Example FC]]}}", "{{Team-Station|1990–1996|[[Elsewhere SC]]}}"))
    assert sorted((r.start, r.role) for r in rows) == [(1996, "youth"), (2001, "youth"), (2003, "first_team")]


def test_open_ended_row_is_only_valid_as_the_last_row():
    rows, _, problems = parsed("{{Team-Station|2001–|[[Example FC]]}}", "{{Team-Station|2005–|[[Example FC]]}}")
    assert [r.start for r in rows] == [2005]
    assert [reason for *_, reason in problems] == ["open-ended but not the current club"]


def test_unreadable_senior_rows_are_reported_and_other_rows_only_counted():
    rows, counts, problems = parsed("{{Team-Station||[[Example FC]]}}", "{{Team-Station|??|[[Example FC II]]}}",
                                    "{{Team-Station|2005–2006|[[Elsewhere SC]]}}")
    assert rows == []
    assert [reason for *_, reason in problems] == ["years unreadable"]
    assert counts["dewiki_player_rows_unreadable_non_senior"] == 1


def test_pages_without_career_table_are_counted():
    counts = Counter()
    assert player_rows({"P": Page("P", 1, 1, "No infobox.")}, {"P": "Q1"}, PLAYER_TITLES, counts, []) == []
    assert counts["dewiki_player_pages_without_career_table"] == 1


def test_current_row_followed_by_reserve_row_of_the_same_club_is_valid():
    rows, _, problems = parsed("{{Team-Station|2011–|[[Example FC]]|407 (45)}}",
                               "{{Team-Station|2013|[[Example FC#Second team|Example FC II]]|2 {{0}}(0)}}",
                               "{{Team-Station|2014–2015|[[Example FC II]]}}")
    assert problems == []
    assert [(r.start, r.end, r.role, r.apps) for r in rows] == [
        (2011, None, "first_team", 407), (2013, 2013, "reserve", 2), (2014, 2015, "reserve", None)]


def test_current_row_followed_by_a_loan_out_is_valid():
    rows, _, problems = parsed("{{Team-Station|2019–|[[Example FC]]|50 (5)}}",
                               "{{Team-Station|2021|→ [[Elsewhere SC]] (Leihe)|10 (1)}}",
                               "{{Team-Station|2022|[[Elsewhere SC]]|10 (1)|leihe=1}}")
    assert problems == []
    assert [(r.start, r.end, r.role) for r in rows] == [(2019, None, "first_team")]


def test_open_ended_row_followed_by_a_senior_row_elsewhere_is_still_rejected():
    rows, _, problems = parsed("{{Team-Station|2011–|[[Example FC]]|407 (45)}}",
                               "{{Team-Station|2013–2015|[[Elsewhere SC]]|2 (0)}}")
    assert rows == []
    assert [reason for *_, reason in problems] == ["open-ended but not the current club"]


def test_open_ended_row_followed_by_a_first_team_row_of_the_same_club_is_rejected():
    _, _, problems = parsed("{{Team-Station|2011–|[[Example FC]]}}", "{{Team-Station|2013–2015|[[Example FC]]}}")
    assert len(problems) == 1


def test_unreadable_senior_rows_are_collected_per_person_and_club():
    unreadable = set()
    counts = Counter()
    player_rows({"P": Page("P", 1, 10, career("{{Team-Station||[[Example FC]]}}"))}, {"P": "Q990000100"},
                PLAYER_TITLES, counts, [], unreadable)
    assert unreadable == {("Q990000100", CLUB)}


@pytest.mark.parametrize("team", ["Example FC Amat.", "[[Example FC]] Am.", "Example FC (Am)", "Example FC (Am.)",
                                  "[[Example FC|Example FC Amat.]]", "[[Example FC#Amateurmannschaft|Example FC]]"])
def test_amateur_abbreviations_are_reserve(team):
    rows, _, problems = parsed(f"{{{{Team-Station|2005–2006|{team}|3 (0)}}}}")
    assert problems == []
    assert [r.role for r in rows] == ["reserve"]


def test_section_anchor_naming_the_first_team_is_first_team():
    rows, _, problems = parsed("{{Team-Station|2005–2006|[[Example FC#Erste Mannschaft|Example FC]]|3 (0)}}",
                               "{{Team-Station|2007–2008|[[Example FC#Profis|Example FC]]|3 (0)}}")
    assert problems == [] and [r.role for r in rows] == ["first_team", "first_team"]


def test_unknown_section_anchor_is_unreadable_and_marks_the_pair():
    unreadable, counts = set(), Counter()
    rows = player_rows({"P": Page("P", 1, 10, career("{{Team-Station|2005–2006|[[Example FC#Geschichte|Example FC]]}}"))},
                       {"P": "Q990000100"}, PLAYER_TITLES, counts, problems := [], unreadable)
    assert rows == [] and unreadable == {("Q990000100", CLUB)}
    assert [reason for *_, reason in problems] == ["section anchor not recognized"]


def test_senior_table_that_is_not_literal_marks_every_club_of_the_youth_table():
    text = ("{{Infobox Fußballspieler\n| jugendvereine_tabelle =\n{{Team-Station|2000–2005|[[Example FC]]}}\n"
            "| vereine_tabelle = ABFRAGE_WIKIDATA\n| nationalmannschaft_tabelle =\n}}\nPlaceholder.")
    unreadable, counts = set(), Counter()
    rows = player_rows({"P": Page("P", 1, 10, text)}, {"P": "Q990000100"}, PLAYER_TITLES, counts, [], unreadable)
    assert [r.role for r in rows] == ["youth"]
    assert unreadable == {("Q990000100", CLUB)}
    assert counts["dewiki_player_pages_senior_table_not_literal"] == 1


def test_literal_senior_table_does_not_mark_the_youth_clubs():
    unreadable = set()
    player_rows({"P": Page("P", 1, 10, career("{{Team-Station|2005–2006|[[Example FC]]}}",
                                              youth=("{{Team-Station|2000–2005|[[Example FC]]}}",)))},
                {"P": "Q990000100"}, PLAYER_TITLES, Counter(), [], unreadable)
    assert unreadable == set()
