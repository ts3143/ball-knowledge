"""The placeholder world the database tests build inside a rolled-back transaction: a competition, five clubs, a
country and their seasons, with helpers to add people, spells and facts and to run prompt generation. Needs the
local database."""

from datetime import date

import psycopg
import pytest
from psycopg.rows import dict_row

from ingest.config import load_settings
from prompts.generate import generate

TUNABLES = {
    "prompts": {"open_ended_current_seasons": 2},
    "ingest": {"start_year": 2000, "coverage": {"teams_per_season": 3}},
    "tiers": {"names": ["Bandwagon", "Casual", "Real One", "Deep Cut", "You Know Ball"],
              "cumulative_share": [0.50, 0.75, 0.90, 0.98, 1.00], "points": [10, 25, 50, 75, 100],
              "hipster_pick_points": 15},
    "quality_gates": {"min_answers": 1, "max_answers": 300, "report_min_answers_alternatives": [1, 2]},
}


def rarity_tunables(competition_qid, **model):
    """The rarity tunables of the placeholder world; `model` overrides [rarity.model] keys."""
    return {
        "fame_weights": {competition_qid: {"de": 1.0, "en": 0.4}},
        "features": {"min_spell_seasons": 0.5, "imputed_apps_per_season": 16, "edge_season_weight": 0.5},
        "model": {"alpha": 0.6, "beta": 0.5, "half_life_seasons": 10, "fame_floor_views": 100, **model},
        "tier_gate": {"max_bandwagon_answer_share": 0.34}, "weak_answer_max_tier": 3,
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
    def __init__(self, conn, seasons=None):
        self.conn = conn
        self.cur = conn.cursor(row_factory=dict_row)
        self.n = 0
        self.comp = self.entity("competition", "Test League")
        self.clubs = {c: self.entity("club", f"Test Club {c}") for c in "ABCDE"}
        self.country = self.entity("country", "Test Country")
        for year, letters in (seasons or SEASONS).items():
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
        tunables = {**TUNABLES, "rarity": rarity_tunables(self.comp["wikidata_id"]), **overrides}
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
