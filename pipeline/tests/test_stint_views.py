"""stints / stint_seasons functions on placeholder rows inside a rolled-back transaction. Needs the local database."""

from datetime import date

import psycopg
import pytest

from ingest.config import load_settings

pytestmark = pytest.mark.db

# Placeholder seasons: 2008-09 .. 2012-13, Aug 1 to May 31 (no real fixtures or facts).
FIRST_SEASON, LAST_SEASON = 2008, 2012
# Open-ended spells starting at or after this year are current. The default is past every placeholder start,
# so existing cases see no current spells.
CUTOFF = 2100


@pytest.fixture
def cur():
    try:
        conn = psycopg.connect(load_settings().database_url, connect_timeout=3)
    except (psycopg.OperationalError, SystemExit) as exc:
        pytest.skip(f"local database not reachable: {exc}")
    try:
        yield conn.cursor()
    finally:
        conn.rollback()
        conn.close()


class Fixture:
    def __init__(self, cur):
        self.cur = cur
        self.n = 0
        self.club = self.entity("club", "Test Club Q990000101")
        self.comp = self.entity("competition", "Test League Q990000102")
        self.person = self.entity("person", "Test Person Q990000103")
        for year in range(FIRST_SEASON, LAST_SEASON + 1):
            self.season(self.club, year)

    def entity(self, type_, name):
        self.n += 1
        cur = self.cur.execute(
            "insert into entities (type, wikidata_id, canonical_name) values (%s, %s, %s) returning id",
            (type_, f"Q99000{self.n:04d}", name),
        )
        return cur.fetchone()[0]

    def season(self, club, year):
        self.n += 1
        self.cur.execute(
            "insert into facts (subject_id, predicate, object_id, start_year, end_year, start_date, end_date,"
            " date_quality, qualifiers, source_ref) values (%s, 'competed_in', %s, %s, %s, %s, %s, 'dated',"
            " %s, %s)",
            (club, self.comp, year, year + 1, date(year, 8, 1), date(year + 1, 5, 31),
             '{"season": "Q990000199"}', f"test:season:{self.n}"),
        )

    def stint(self, predicate="played_for", club=None, quality="dated", source="wikidata", stale=False, **cols):
        self.n += 1
        cols = {"start_year": None, "end_year": None, "start_date": None, "start_precision": None,
                "end_date": None, "end_precision": None, "apps": 5, **cols}
        names = ", ".join(cols)
        marks = ", ".join(["%s"] * len(cols))
        self.cur.execute(
            f"insert into facts (subject_id, predicate, object_id, date_quality, source, stale, source_ref,"
            f" {names}) values (%s, %s, %s, %s, %s, %s, %s, {marks})",
            (self.person, predicate, club or self.club, quality, source, stale, f"test:stint:{self.n}",
             *cols.values()),
        )

    def seasons(self, predicate="played_for", cutoff=CUTOFF):
        rows = self.cur.execute(
            "select season_start_year, edge, weak_reasons from stint_seasons(%s)"
            " where person_id = %s and predicate = %s order by 1",
            (cutoff, self.person, predicate),
        ).fetchall()
        return {year: (edge, weak) for year, edge, weak in rows}

    def stints(self, cutoff=CUTOFF):
        return self.cur.execute("select fact_id from stints(%s) where person_id = %s",
                                (cutoff, self.person)).fetchall()


@pytest.fixture
def fx(cur):
    return Fixture(cur)


def test_year_precision_interior_certain_boundaries_edge(fx):
    fx.stint(start_year=2009, end_year=2011, start_precision=9, end_precision=9, start_date=date(2009, 1, 1),
             end_date=date(2011, 1, 1))
    got = fx.seasons()
    assert {y: e for y, (e, _) in got.items()} == {2008: True, 2009: False, 2010: False, 2011: True}


def test_year_precision_example_across_window(fx):
    fx.season(fx.club, 2006)
    fx.season(fx.club, 2007)
    fx.season(fx.club, 2013)
    fx.season(fx.club, 2014)
    fx.stint(start_year=2008, end_year=2012, start_precision=9, end_precision=9, start_date=date(2008, 1, 1),
             end_date=date(2012, 1, 1))
    got = {y: e for y, (e, _) in fx.seasons().items()}
    assert got == {2007: True, 2008: False, 2009: False, 2010: False, 2011: False, 2012: True}
    assert 2006 not in got and 2013 not in got


def test_summer_arrival_does_not_cover_previous_season(fx):
    fx.stint(start_year=2010, start_date=date(2010, 7, 1), start_precision=11, end_year=2011,
             end_date=date(2011, 12, 31), end_precision=11)
    got = fx.seasons()
    assert 2009 not in got
    assert got[2010] == (False, [])


def test_summer_departure_does_not_cover_next_season(fx):
    fx.stint(start_year=2009, start_date=date(2009, 8, 15), start_precision=11, end_year=2011,
             end_date=date(2011, 6, 30), end_precision=11)
    got = fx.seasons()
    assert 2011 not in got
    assert got[2010][0] is False


def test_month_precision_end_in_november_covers_season_certainly(fx):
    fx.stint(start_year=2009, start_date=date(2009, 1, 1), start_precision=10, end_year=2011,
             end_date=date(2011, 11, 1), end_precision=10)
    got = fx.seasons()
    assert got[2011] == (False, [])
    assert 2012 not in got
    assert got[2009][0] is False


def test_undated_covers_all_seasons_weak(fx):
    fx.stint(quality="undated")
    got = fx.seasons()
    assert sorted(got) == list(range(FIRST_SEASON, LAST_SEASON + 1))
    assert all(weak == ["undated"] for _, weak in got.values())


def test_open_ended_covers_from_start_weak(fx):
    fx.stint(quality="open_ended", start_year=2010, start_date=date(2010, 1, 1), start_precision=9)
    got = fx.seasons()
    assert sorted(got) == [2009, 2010, 2011, 2012]
    assert got[2010] == (False, ["open_ended"])
    assert got[2009][0] is True


def test_open_ended_starting_at_the_cutoff_is_current(fx):
    fx.stint(quality="open_ended", start_year=2010, start_date=date(2010, 1, 1), start_precision=9)
    got = fx.seasons(cutoff=2010)
    assert sorted(got) == [2009, 2010, 2011, 2012]
    assert got[2010] == (False, [])
    assert fx.seasons(cutoff=2011)[2010] == (False, ["open_ended"])


def test_open_ended_starting_after_the_cutoff_is_current(fx):
    fx.stint(quality="open_ended", start_year=2011, start_date=date(2011, 1, 1), start_precision=9)
    assert fx.seasons(cutoff=2010)[2011] == (False, [])


def test_open_ended_starting_before_the_cutoff_is_weak(fx):
    fx.stint(quality="open_ended", start_year=2009, start_date=date(2009, 1, 1), start_precision=9)
    assert all(weak == ["open_ended"] for _, weak in fx.seasons(cutoff=2010).values())


def test_open_ended_without_a_start_year_is_weak(fx):
    fx.stint(quality="open_ended")
    assert all(weak == ["open_ended"] for _, weak in fx.seasons(cutoff=2010).values())


def test_undated_stays_weak_whatever_the_cutoff(fx):
    fx.stint(quality="undated")
    assert all(weak == ["undated"] for _, weak in fx.seasons(cutoff=2000).values())


def test_stint_seasons_expose_date_quality(fx):
    fx.stint(quality="undated")
    fx.stint(start_year=2009, end_year=2010, start_precision=9, end_precision=9, start_date=date(2009, 1, 1),
             end_date=date(2010, 1, 1))
    got = {q for (q,) in fx.cur.execute(
        "select date_quality from stint_seasons(%s) where person_id = %s", (CUTOFF, fx.person)).fetchall()}
    assert got == {"undated", "dated"}


def test_functions_are_not_executable_by_the_data_api_roles(fx):
    for role in ("anon", "authenticated", "public"):
        for fn in ("stints(int)", "stint_seasons(int)"):
            assert fx.cur.execute("select has_function_privilege(%s, %s, 'execute')", (role, fn)).fetchone() == (False,)


def test_apps_weak_reasons_only_for_played_for(fx):
    fx.stint(quality="undated", apps=None)
    fx.stint(quality="undated", apps=0)
    fx.stint(predicate="coached", quality="undated", apps=None)
    reasons = {(r[0], r[1], tuple(r[2])) for r in fx.cur.execute(
        "select predicate, apps, weak_reasons from stints(%s) where person_id = %s", (CUTOFF, fx.person)).fetchall()}
    assert ("played_for", None, ("undated", "no_apps")) in reasons
    assert ("played_for", 0, ("undated", "zero_apps")) in reasons
    assert ("coached", None, ("undated",)) in reasons


def test_stale_facts_excluded(fx):
    fx.stint(quality="undated", stale=True)
    assert fx.stints() == []
    assert fx.seasons() == {}


def test_dewiki_coached_row_behaves_as_year_precision(fx):
    fx.stint(predicate="coached", source="dewiki", start_year=2009, end_year=2011, apps=None)
    row = fx.cur.execute(
        "select earliest_start, latest_start, earliest_end, latest_end, weak_reasons from stints(%s)"
        " where person_id = %s", (CUTOFF, fx.person)).fetchone()
    assert row == (date(2009, 1, 1), date(2009, 12, 31), date(2011, 1, 1), date(2011, 12, 31), [])
    got = {y: e for y, (e, _) in fx.seasons("coached").items()}
    assert got == {2008: True, 2009: False, 2010: False, 2011: True}


def test_season_club_did_not_compete_yields_no_row(fx):
    other = fx.entity("club", "Test Club Q990000104")
    fx.stint(club=other, quality="undated")
    assert len(fx.stints()) == 1
    assert fx.seasons() == {}
