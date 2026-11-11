"""Infobox coaching-table parsing on synthetic wikitext. Club and page names are placeholders."""

from collections import Counter

import pytest

from ingest.dewiki import Page, coach_stations, match_club, parse_stations, parse_years, station_role

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
