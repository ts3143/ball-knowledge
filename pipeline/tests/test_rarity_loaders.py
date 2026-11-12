"""Rarity feature loaders on placeholder rows inside a rolled-back transaction. Needs the local database."""

from datetime import date

import pytest

from rarity.features import AnswerSet, compute_features, load_evidence
from tests.db_world import TUNABLES, World

pytestmark = pytest.mark.db


def tunables(world):
    return {**TUNABLES, "rarity": {
        "fame_weights": {world.comp["wikidata_id"]: {"de": 1.0, "en": 0.4}},
        "features": {"min_spell_seasons": 0.5, "imputed_apps_per_season": 16, "edge_season_weight": 0.5}}}


def test_loaders_return_what_the_feature_functions_expect(conn):
    w = World(conn)  # seasons 2008..2013 of clubs A-E, so R = 2013 and the current cutoff is 2012
    a, b = w.clubs["A"]["id"], w.clubs["B"]["id"]
    p1, p2 = w.person("Placeholder One"), w.person("Placeholder Two")
    dated = w.stint(p1, "A", apps=20, start_year=2010, end_year=2012)
    current = w.stint(p1, "B", apps=7, start_year=2012, end_year=None, end_precision=None,
                      date_quality="open_ended")
    undated = w.stint(p2, "A", apps=None, start_year=None, end_year=None, start_precision=None,
                      end_precision=None, date_quality="undated")
    won = w.fact(a, "won", w.comp["id"], start_year=2010)
    for month, views in ((date(2020, 1, 1), 100), (date(2020, 2, 1), 50)):
        w.cur.execute("insert into entity_pageviews (entity_id, wiki, month, views) values (%s, 'de', %s, %s)",
                      (p1["id"], month, views))
    w.cur.execute("insert into entity_pageviews (entity_id, wiki, month, views) values (%s, 'en', %s, 10)",
                  (p1["id"], date(2020, 1, 1)))

    sets = [AnswerSet("club_pair", [a, b], {p1["id"]: [dated, current, won]}),
            AnswerSet("club_nationality", [a], {p2["id"]: [undated]})]
    ev = load_evidence(w.cur, sets, w.comp["wikidata_id"], tunables(w))

    assert (ev.params.reference_season, ev.params.ref_date) == (2013, date(2014, 6, 30))
    assert ev.views == {p1["id"]: (150, 10)}
    # +-infinity bounds come back as None
    assert ev.stints[undated].earliest_start is None and ev.stints[undated].latest_end is None
    assert ev.stints[current].latest_end is None and ev.stints[current].earliest_start == date(2012, 1, 1)
    assert ev.stints[dated].weak_reasons == ()
    assert {r.season: r.edge for r in ev.season_rows[dated]} == {2009: True, 2010: False, 2011: False, 2012: True}
    assert (ev.events[won].predicate, ev.events[won].club_id, ev.events[won].season) == ("won", a, 2010)

    pair = compute_features(sets[0], ev)[p1["id"]]
    assert pair.tie_amount == 7  # the weaker link
    assert pair.seasons_since == 0.0  # the Test Club B spell is current
    assert (pair.views_de, pair.views_en, pair.fame) == (150, 10, 154.0)
    nat = compute_features(sets[1], ev)[p2["id"]]
    assert nat.tie_amount == 16 and nat.apps_imputed and nat.recency_unknown
    assert nat.seasons_since == (2013 + 1 - 2000) / 2


def test_missing_fame_weights_for_the_competition_stop_the_run(conn):
    w = World(conn)
    bad = {**tunables(w), "rarity": {**tunables(w)["rarity"], "fame_weights": {}}}
    with pytest.raises(SystemExit, match="fame_weights"):
        load_evidence(w.cur, [AnswerSet("club_pair", [], {})], w.comp["wikidata_id"], bad)
