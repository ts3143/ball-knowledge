import json
import tomllib
from datetime import date

import pytest

from rarity.features import (
    AnswerSet,
    EventFact,
    Evidence,
    FeatureParams,
    SeasonRow,
    Stint,
    apps_at,
    coach_tenure_years,
    compute_features,
    drop_duplicates,
    fame,
    midpoint,
    seasons_since_club,
    spell_bounds,
    spell_end,
    tenure_years,
    usable_season_rows,
    window_fraction,
)
from rarity.preview import StoredPrompt, coverage_line, render_gut_sheet, select_for_gut_sheet

# Reference season 2025: ref date 2026-06-30. M = 2 * (18 - 1) = 34.
P = FeatureParams(min_spell_seasons=0.5, imputed_apps_per_season=16, edge_season_weight=0.5,
                  matches_per_season=34, reference_season=2025, default_seasons_since=13.0, window_start=date(2000, 1, 1))
A, B = 1, 2  # club ids


def stint(fact_id=1, club=A, start=2010, end=2012, apps=10, quality="dated", weak=(), **cols):
    """A dated stint with year precision unless the dates are given."""
    cols = {
        "earliest_start": date(start, 1, 1) if start else None, "latest_start": date(start, 12, 31) if start else None,
        "earliest_end": date(end, 1, 1) if end else None, "latest_end": date(end, 12, 31) if end else None, **cols}
    return Stint(fact_id=fact_id, club_id=club, start_year=start, end_year=end, apps=apps, date_quality=quality,
                 weak_reasons=tuple(weak), **cols)


def evidence(stints=(), rows=(), events=(), views=None):
    season_rows: dict[int, list[SeasonRow]] = {}
    for r in rows:
        season_rows.setdefault(r.fact_id, []).append(r)
    return Evidence(P, {s.fact_id: s for s in stints}, season_rows, {e.fact_id: e for e in events},
                    views or {}, {"de": 1.0, "en": 0.4})


def rows(fact_id, club, seasons, edge=()):
    return [SeasonRow(fact_id, club, y, y in edge) for y in seasons]


def test_fame_weights_the_editions_and_a_missing_edition_adds_nothing():
    assert fame(1000, 500, {"de": 1.0, "en": 0.4}) == 1200
    assert fame(0, 500, {"de": 1.0, "en": 0.4}) == 200
    assert fame(0, 0, {"de": 1.0, "en": 0.4}) == 0


def test_midpoint():
    assert midpoint(date(2010, 1, 1), date(2010, 12, 31)) == date(2010, 7, 2)
    assert midpoint(date(2010, 5, 5), date(2010, 5, 5)) == date(2010, 5, 5)
    assert midpoint(None, date(2010, 5, 5)) is None


def test_dated_spell_bounds_and_tenure():
    s = stint(start=2010, end=2012)
    start, end = spell_bounds(s, P.ref_date)
    assert (start.year, end.year) == (2010, 2012)
    assert tenure_years(s, P) == pytest.approx(2.0, abs=0.01)


def test_tenure_has_a_floor():
    assert tenure_years(stint(start=2010, end=2010), P) == 0.5


def test_undated_and_no_start_spells_have_unknown_bounds():
    assert spell_bounds(stint(start=None, end=None, quality="undated", weak=["undated"]), P.ref_date) is None
    assert spell_bounds(stint(start=None, end=2012, weak=["no_start"]), P.ref_date) is None
    # the undated default: the tenure floor, and recency falls back to the default
    undated = stint(start=None, end=None, quality="undated", apps=None, weak=["undated", "no_apps"])
    assert tenure_years(undated, P) == 0.5
    assert seasons_since_club([undated], P) == (13.0, True)


def test_current_spell_gives_zero_seasons_since():
    current = stint(start=2023, end=None, quality="open_ended", latest_end=None, earliest_end=None)
    assert spell_bounds(current, P.ref_date)[1] == P.ref_date
    assert seasons_since_club([current], P) == (0.0, False)


def test_weak_open_ended_spell_lasts_one_season_not_until_now():
    weak = stint(start=2005, end=None, quality="open_ended", weak=["open_ended"], latest_end=None,
                 earliest_end=None)
    start, end = spell_bounds(weak, P.ref_date)
    assert (end - start).days == 365
    assert tenure_years(weak, P) == pytest.approx(1.0, abs=0.01)
    since, unknown = seasons_since_club([weak], P)
    assert not unknown and since == pytest.approx(20.0, abs=0.5)


def test_seasons_since_uses_the_latest_end_and_never_goes_negative():
    old, recent = stint(1, start=2005, end=2007), stint(2, start=2010, end=2014)
    assert seasons_since_club([old, recent], P)[0] == pytest.approx(12.0, abs=0.1)
    future = stint(start=2025, end=2030)
    assert seasons_since_club([future], P)[0] == 0.0


def test_duplicate_inside_another_with_no_higher_apps_is_dropped():
    outer, inner = stint(1, start=2010, end=2014, apps=100), stint(2, start=2011, end=2012, apps=40)
    kept, dropped = drop_duplicates([outer, inner])
    assert kept == [outer] and dropped == 1


def test_inner_spell_with_more_apps_than_the_outer_is_kept():
    outer, inner = stint(1, start=2010, end=2014, apps=10), stint(2, start=2011, end=2012, apps=40)
    assert drop_duplicates([outer, inner]) == ([outer, inner], 0)


def test_identical_spells_keep_the_lower_fact_id_or_the_higher_apps():
    a, b = stint(5, apps=30), stint(3, apps=30)
    assert drop_duplicates([a, b]) == ([b], 1)
    assert drop_duplicates([stint(1, apps=None), stint(2, apps=30)]) == ([stint(2, apps=30)], 1)


def test_spells_that_merely_touch_or_do_not_overlap_are_summed():
    first, touching = stint(1, start=2008, end=2010, apps=20), stint(2, start=2010, end=2012, apps=30)
    apart = stint(3, start=2014, end=2015, apps=5)
    kept, dropped = drop_duplicates([first, touching, apart])
    assert (kept, dropped) == ([first, touching, apart], 0)
    assert apps_at([first, touching, apart], P) == (55, False, 0)


def test_null_apps_are_imputed_from_the_tenure_and_zero_apps_stay_zero():
    # 2 years at 16 a season
    apps, imputed, _ = apps_at([stint(apps=None)], P)
    assert apps == pytest.approx(32.0, abs=0.2) and imputed
    assert apps_at([stint(apps=0)], P) == (0, False, 0)
    # a spell shorter than a season is imputed a full season
    assert apps_at([stint(start=2010, end=2010, apps=None)], P) == (16, True, 0)


def test_club_pair_uses_the_weaker_tie_and_the_most_recent_association():
    stints = [stint(1, A, start=2008, end=2012, apps=200), stint(2, B, start=2020, end=2022, apps=15)]
    ev = evidence(stints, views={7: (1000, 500)})
    f = compute_features(AnswerSet("club_pair", [A, B], {7: [1, 2]}), ev)[7]
    assert f.tie_amount == 15
    assert f.seasons_since == pytest.approx(4.0, abs=0.1)  # B ended mid 2022; min over the clubs
    assert (f.views_de, f.views_en, f.fame) == (1000, 500, 1200)
    assert not f.apps_imputed and not f.recency_unknown


def test_club_pair_counts_duplicates_dropped_at_both_clubs():
    stints = [stint(1, A, start=2008, end=2012, apps=90), stint(2, A, start=2009, end=2010, apps=30),
              stint(3, B, start=2013, end=2014, apps=20)]
    f = compute_features(AnswerSet("club_pair", [A, B], {7: [1, 2, 3]}), evidence(stints))[7]
    assert f.tie_amount == 20 and f.duplicates_dropped == 1


def test_club_nationality_ignores_stints_at_other_clubs_and_non_stint_evidence():
    stints = [stint(1, A, start=2008, end=2012, apps=50), stint(2, B, start=2012, end=2014, apps=99)]
    f = compute_features(AnswerSet("club_nationality", [A], {7: [1, 2, 900]}), evidence(stints))[7]
    assert f.tie_amount == 50


def test_coach_tenure_is_the_union_of_spells_with_a_floor_and_m_converts_to_matches():
    wikidata, dewiki = stint(1, start=2010, end=2012, apps=None), stint(2, start=2010, end=2012, apps=None)
    assert coach_tenure_years([wikidata, dewiki], P) == pytest.approx(2.0, abs=0.01)  # counted once
    interim = stint(3, start=2013, end=2013, apps=None)
    assert coach_tenure_years([interim], P) == pytest.approx(0.5, abs=0.01)
    f = compute_features(AnswerSet("club_coaches", [A], {7: [1, 2]}), evidence([wikidata, dewiki]))[7]
    assert f.tie_amount == pytest.approx(2.0 * 34, abs=0.5)
    assert not f.apps_imputed


def test_coach_spells_with_no_dates_count_the_floor_only_when_nothing_is_dated():
    undated = stint(1, start=None, end=None, quality="undated", apps=None)
    assert coach_tenure_years([undated], P) == 0.5
    assert coach_tenure_years([undated, stint(2, start=2010, end=2012)], P) == pytest.approx(2.0, abs=0.01)
    assert coach_tenure_years([], P) == 0.0


def test_weak_open_ended_spell_keeps_only_its_first_season_in_season_templates():
    weak = stint(1, start=2005, end=None, quality="open_ended", weak=["open_ended"])
    ev = evidence([weak], rows(1, A, range(2005, 2026)))
    assert [r.season for r in usable_season_rows([1], ev)] == [2005]


def test_competition_templates_sum_apps_per_season_over_distinct_club_seasons():
    # 3 years, 60 apps -> 20 a season; seasons 2010..2012 (3 distinct pairs), all non-edge
    s = stint(1, start=2010, end=2013, apps=60)
    ev = evidence([s], rows(1, A, [2010, 2011, 2012]))
    f = compute_features(AnswerSet("bundesliga_nationality", [], {7: [1]}), ev)[7]
    assert f.tie_amount == pytest.approx(60.0, abs=0.5) and f.seasons == 3 and f.edge_seasons == 0
    assert f.seasons_since == 2025 - 2012


def test_edge_seasons_count_for_the_edge_weight():
    s = stint(1, start=2010, end=2012, apps=40)  # 20 a season
    ev = evidence([s], rows(1, A, [2009, 2010, 2011, 2012], edge=[2009, 2012]))
    f = compute_features(AnswerSet("bundesliga_nationality", [], {7: [1]}), ev)[7]
    assert f.tie_amount == pytest.approx(0.5 * 20 + 20 + 20 + 0.5 * 20, abs=0.5)
    assert (f.seasons, f.edge_seasons) == (4, 2)


def test_two_spells_covering_one_season_count_the_larger_and_a_non_edge_row_cancels_the_edge():
    low, high = stint(1, start=2010, end=2012, apps=20), stint(2, start=2010, end=2012, apps=60)  # 10 and 30 a season
    ev = evidence([low, high], rows(1, A, [2011], edge=[2011]) + rows(2, A, [2011]))
    f = compute_features(AnswerSet("bundesliga_nationality", [], {7: [1, 2]}), ev)[7]
    assert f.tie_amount == pytest.approx(30.0, abs=0.5) and f.edge_seasons == 0


def test_null_apps_in_a_season_template_use_the_imputed_rate_and_are_flagged():
    s = stint(1, apps=None)
    f = compute_features(AnswerSet("bundesliga_nationality", [], {7: [1]}), evidence([s], rows(1, A, [2011])))[7]
    assert f.tie_amount == 16 and f.apps_imputed


def test_title_winners_count_only_title_seasons_of_the_club():
    s = stint(1, start=2008, end=2013, apps=50)  # 10 a season
    other = stint(2, B, start=2008, end=2013, apps=500)
    ev = evidence([s, other], rows(1, A, range(2008, 2013)) + rows(2, B, range(2008, 2013)),
                  events=[EventFact(10, "won", A, 2009), EventFact(11, "won", A, 2011), EventFact(12, "won", B, 2010)])
    f = compute_features(AnswerSet("club_title_winners", [A], {7: [1, 2, 10, 11]}), ev)[7]
    assert f.seasons == 2 and f.tie_amount == pytest.approx(20.0, abs=0.5)  # 2009 and 2011 at club A only
    assert f.seasons_since == 2025 - 2011


def test_title_winner_with_no_title_season_row_has_no_tie_and_unknown_recency():
    s = stint(1, start=2000, end=2001)
    ev = evidence([s], rows(1, A, [2000]), events=[EventFact(10, "won", A, 2009)])
    f = compute_features(AnswerSet("club_title_winners", [A], {7: [1, 10]}), ev)[7]
    assert f.tie_amount == 0 and f.recency_unknown and f.seasons_since == 13.0


def test_multi_club_coaches_sum_m_over_club_seasons_and_take_the_last_season():
    a, b = stint(1, A, start=2010, end=2012, apps=None), stint(2, B, start=2014, end=2015, apps=None)
    ev = evidence([a, b], rows(1, A, [2010, 2011]) + rows(2, B, [2014], edge=[2014]))
    f = compute_features(AnswerSet("multi_club_coaches", [], {7: [1, 2]}), ev)[7]
    assert f.tie_amount == pytest.approx(34 + 34 + 0.5 * 34)
    assert f.seasons_since == 2025 - 2014 and f.seasons == 3 and f.edge_seasons == 1


def test_club_events_count_evidence_facts_and_take_the_last_season():
    events = [EventFact(10, "competed_in", A, 2003), EventFact(11, "competed_in", A, 2012),
              EventFact(12, "competed_in", A, 2018)]
    for template in ("relegated_clubs", "promoted_clubs"):
        f = compute_features(AnswerSet(template, [], {A: [10, 11, 12]}), evidence(events=events))[A]
        assert f.tie_amount == 3 and f.seasons_since == 2025 - 2018 and not f.recency_unknown


def test_club_event_with_no_known_season_is_flagged():
    f = compute_features(AnswerSet("relegated_clubs", [], {A: []}), evidence())[A]
    assert f.tie_amount == 0 and f.recency_unknown


def test_features_come_back_in_entity_order():
    ev = evidence()
    out = compute_features(AnswerSet("relegated_clubs", [], {9: [], 3: [], 5: []}), ev)
    assert list(out) == [3, 5, 9]


def prompt(pid, template, names):
    return StoredPrompt(pid, template, f"Text {pid}", AnswerSet(template, [], {i: [] for i in names}), {},
                        dict(names))


def test_gut_sheet_selection_is_seeded_and_takes_all_when_there_are_few():
    prompts = [prompt(i, "club_pair", {1: "x"}) for i in range(10)] + [prompt(50, "club_coaches", {1: "x"})]
    first = select_for_gut_sheet(prompts, 2, seed=7)
    assert first == select_for_gut_sheet(prompts, 2, seed=7)
    assert [p.template for p in first] == ["club_pair", "club_pair", "club_coaches"]
    assert [p.id for p in first][:2] == sorted(p.id for p in first[:2])


def test_gut_sheet_lists_answers_alphabetically_with_empty_top_and_bottom_and_no_scores():
    p = prompt(5, "club_pair", {3: "Özil", 1: "Mario Götze", 2: 'Said "Sly" Ahmed', 4: "adam"})
    text = render_gut_sheet([p])
    doc = tomllib.loads(text)
    assert text.startswith("# Gut check")
    [entry] = doc["prompt"]
    assert entry["answers"] == ["adam", "Mario Götze", 'Said "Sly" Ahmed', "Özil"]
    assert entry["top"] == [] and entry["bottom"] == [] and entry["id"] == 5
    assert set(entry) == {"id", "text", "answers", "top", "bottom"}
    assert json.dumps("Mario Götze", ensure_ascii=False) in text


def test_coverage_line_counts_the_diagnostics():
    from rarity.features import Features
    fs = [Features(0, 0, 0.0, 1.0, 1.0, apps_imputed=True, duplicates_dropped=2, seasons=4, edge_seasons=1),
          Features(5, 0, 5.0, 1.0, 1.0, recency_unknown=True)]
    line = coverage_line("x", fs)
    assert "no views      1 (50.0%)" in line and "apps imputed      1" in line
    assert "dups dropped 2 (in 1 answers)" in line and "25.0% of 4 seasons" in line


def test_spells_are_clipped_to_the_window():
    s = stint(start=1998, end=2004, apps=120)  # 6 years; 4 inside 2000-
    start, end = spell_bounds(s, P.ref_date, P.window_start)
    assert start == date(2000, 1, 1) and end.year == 2004
    assert tenure_years(s, P) == pytest.approx(4.5, abs=0.1)
    assert tenure_years(s, P, clip=False) == pytest.approx(6.0, abs=0.1)
    assert window_fraction(s, P) == pytest.approx(0.75, abs=0.02)
    assert window_fraction(stint(start=2005, end=2007), P) == 1.0
    # a spell that ended before the window never gets a start after its end
    assert tenure_years(stint(start=1990, end=1995), P) == 0.5


def test_apps_are_pro_rated_by_the_in_window_fraction_and_imputed_apps_use_the_clipped_tenure():
    s = stint(start=1998, end=2004, apps=120)
    assert apps_at([s], P)[0] == pytest.approx(120 * 0.75, abs=2.5)
    imputed, flagged, _ = apps_at([stint(start=1998, end=2004, apps=None)], P)
    assert flagged and imputed == pytest.approx(16 * 4.5, abs=2.0)


def test_coach_tenure_counts_only_the_window():
    # 1998-2004 is about 4.5 seasons by its midpoints (mid 1998-07 to mid 2004-07 is 6; clipped to 2000-01 is 4.5)
    s = stint(start=1998, end=2004, apps=None)
    assert coach_tenure_years([s], P) == pytest.approx(4.5, abs=0.1)
    f = compute_features(AnswerSet("club_coaches", [A], {7: [1]}), evidence([s]))[7]
    assert f.tie_amount == pytest.approx(4.5 * 34, abs=4)


def test_season_templates_use_the_rate_of_the_whole_spell():
    s = stint(1, start=1998, end=2004, apps=120)  # 20 a season, whatever the window
    f = compute_features(AnswerSet("bundesliga_nationality", [], {7: [1]}), evidence([s], rows(1, A, [2000, 2001])))[7]
    assert f.tie_amount == pytest.approx(40.0, abs=1.0)


def test_no_start_spell_with_a_known_end_takes_its_recency_from_that_end():
    s = stint(start=None, end=2012, weak=["no_start"], apps=None)
    assert spell_bounds(s, P.ref_date) is None and spell_end(s, P.ref_date).year == 2012
    since, unknown = seasons_since_club([s], P)
    assert not unknown and since == pytest.approx(14.0, abs=0.1)
    # tenure and apps imputation are unchanged: the floor, imputed at one season
    assert tenure_years(s, P) == 0.5 and apps_at([s], P) == (16, True, 0)
    # no end at all stays unknown
    open_end = stint(start=None, end=None, weak=["no_start"], latest_end=None, earliest_end=None)
    assert spell_end(open_end, P.ref_date) is None
    assert spell_end(stint(start=None, end=None, quality="undated", weak=["undated"]), P.ref_date) is None
