"""Obviousness features per answer: fame (pageviews), tie (how much the answer is tied to the prompt) and
recency (seasons since the last tie). Pure functions over rows the loaders read in bulk; nothing here
writes to the database or reads the clock (the reference date is the competition's latest season).

Tie and recency per template:
- club_pair: tie is the smaller of the apps at the two clubs (the weaker link limits recall); recency is the
  more recent association.
- club_nationality: apps at the club. club_coaches: seasons coached at the club, as match equivalents.
- bundesliga_nationality: apps in the competition's seasons. club_title_winners: the same, in title seasons only.
- multi_club_coaches: match equivalents over the (club, season) pairs coached.
- relegated_clubs / promoted_clubs: the number of events.
A spell's apps are spread over the seasons it possibly covers, so a person's tie never exceeds their apps.
"""

import math
from dataclasses import dataclass
from datetime import date, timedelta

DAYS_PER_YEAR = 365.25
_NEG, _POS = -math.inf, math.inf


@dataclass(frozen=True)
class AnswerSet:
    template_id: str
    club_ids: list[int]  # the prompt's club tags, in template parameter order
    answers: dict[int, list[int]]  # entity_id -> evidence fact ids


@dataclass(frozen=True)
class Stint:
    """One row of stints(); bounds are None where the function returned +-infinity."""
    fact_id: int
    club_id: int
    start_year: int | None
    end_year: int | None
    apps: int | None
    date_quality: str
    earliest_start: date | None
    latest_start: date | None
    earliest_end: date | None
    latest_end: date | None
    weak_reasons: tuple[str, ...]


@dataclass(frozen=True)
class SeasonRow:
    """One row of stint_seasons(), restricted to the competition."""
    fact_id: int
    club_id: int
    season: int
    edge: bool


@dataclass(frozen=True)
class EventFact:
    """A `won` or `competed_in` fact: the club (subject) and the season it starts."""
    fact_id: int
    predicate: str
    club_id: int
    season: int | None


@dataclass(frozen=True)
class FeatureParams:
    min_spell_seasons: float
    imputed_apps_per_season: float
    edge_season_weight: float
    matches_per_season: int  # M: coach seasons -> appearance equivalents
    reference_season: int  # R
    default_seasons_since: float  # when no spell has a usable end date
    window_start: date  # prompts say "since {start_year}": only the part of a spell from here on counts

    @property
    def ref_date(self) -> date:
        return date(self.reference_season + 1, 6, 30)

    @classmethod
    def from_tunables(cls, tunables: dict, reference_season: int) -> "FeatureParams":
        f = tunables["rarity"]["features"]
        teams = tunables["ingest"]["coverage"]["teams_per_season"]
        return cls(
            min_spell_seasons=f["min_spell_seasons"], imputed_apps_per_season=f["imputed_apps_per_season"],
            edge_season_weight=f["edge_season_weight"], matches_per_season=2 * (teams - 1),
            reference_season=reference_season,
            default_seasons_since=(reference_season + 1 - tunables["ingest"]["start_year"]) / 2,
            window_start=date(tunables["ingest"]["start_year"], 1, 1),
        )


@dataclass(frozen=True)
class Evidence:
    """Everything the feature functions need for a batch of answer sets, loaded once."""
    params: FeatureParams
    stints: dict[int, Stint]
    season_rows: dict[int, list[SeasonRow]]  # by stint fact id
    events: dict[int, EventFact]
    views: dict[int, tuple[int, int]]  # entity_id -> (de, en); absent = no pageviews at all
    fame_weights: dict[str, float]


@dataclass(frozen=True)
class Features:
    views_de: int
    views_en: int
    fame: float  # V
    tie_amount: float
    seasons_since: float
    apps_imputed: bool = False
    recency_unknown: bool = False
    duplicates_dropped: int = 0
    seasons: int = 0  # (club, season) pairs counted, season templates only
    edge_seasons: int = 0  # of those, the ones weighted down because every spell only touches them


# ---------------------------------------------------------------------------
# Loaders (read-only SQL, once per run)
# ---------------------------------------------------------------------------

def latest_season(cur, competition_id: int, competition_qid: str) -> int:
    """The competition's latest season_start_year in the data. `cur` must be a dict_row cursor."""
    cur.execute("select max(season_start_year) as y from club_seasons where competition_id = %s",
                (competition_id,))
    latest = cur.fetchone()["y"]
    if latest is None:
        raise SystemExit(f"No seasons for {competition_qid}; run the ingest first.")
    return latest


def current_from_year(cur, competition_id: int, competition_qid: str, tunables: dict) -> int:
    """Open-ended spells from this season on are current, not weak: the latest season in the data minus the
    tunable window (rendered as a literal into each stored answer_query). `cur` must be a dict_row cursor."""
    latest = latest_season(cur, competition_id, competition_qid)
    return latest - (tunables["prompts"]["open_ended_current_seasons"] - 1)


def load_evidence(cur, answer_sets: list[AnswerSet], competition_qid: str, tunables: dict) -> Evidence:
    """Reads pageviews, stints, season rows and won / competed_in facts for every answer in the sets.
    `cur` must be a dict_row cursor."""
    cur.execute("select id from entities where wikidata_id = %s and type = 'competition'", (competition_qid,))
    comp = cur.fetchone()
    if comp is None:
        raise SystemExit(f"No competition entity {competition_qid}; run the ingest first.")
    reference = latest_season(cur, comp["id"], competition_qid)
    params = FeatureParams.from_tunables(tunables, reference)
    weights = tunables["rarity"]["fame_weights"].get(competition_qid)
    if weights is None:
        raise SystemExit(f"No [rarity.fame_weights.{competition_qid}] in tunables.toml.")
    current_from = current_from_year(cur, comp["id"], competition_qid, tunables)

    entity_ids = sorted({e for s in answer_sets for e in s.answers})
    fact_ids = sorted({f for s in answer_sets for ids in s.answers.values() for f in ids})

    cur.execute("select entity_id, wiki, sum(views)::bigint as views from entity_pageviews"
                " where entity_id = any(%s) group by entity_id, wiki", (entity_ids,))
    views: dict[int, tuple[int, int]] = {}
    for r in cur.fetchall():
        de, en = views.get(r["entity_id"], (0, 0))
        views[r["entity_id"]] = (r["views"], en) if r["wiki"] == "de" else (de, r["views"])

    # +-infinity dates cannot be loaded by psycopg; they come back as NULL.
    cur.execute(
        "select fact_id, club_id, start_year, end_year, apps, date_quality, weak_reasons,"
        " case when isfinite(earliest_start) then earliest_start end as earliest_start,"
        " case when isfinite(latest_start) then latest_start end as latest_start,"
        " case when isfinite(earliest_end) then earliest_end end as earliest_end,"
        " case when isfinite(latest_end) then latest_end end as latest_end"
        " from stints(%s) where fact_id = any(%s)", (current_from, fact_ids))
    stints = {r["fact_id"]: Stint(**{**r, "weak_reasons": tuple(r["weak_reasons"])}) for r in cur.fetchall()}

    cur.execute("select fact_id, club_id, season_start_year as season, edge from stint_seasons(%s)"
                " where fact_id = any(%s) and competition_id = %s order by fact_id, season",
                (current_from, fact_ids, comp["id"]))
    season_rows: dict[int, list[SeasonRow]] = {}
    for r in cur.fetchall():
        season_rows.setdefault(r["fact_id"], []).append(SeasonRow(**r))

    cur.execute("select id as fact_id, predicate, subject_id as club_id, start_year as season from facts"
                " where id = any(%s) and predicate in ('won', 'competed_in')", (fact_ids,))
    events = {r["fact_id"]: EventFact(**r) for r in cur.fetchall()}
    return Evidence(params, stints, season_rows, events, views, dict(weights))


# ---------------------------------------------------------------------------
# Fame
# ---------------------------------------------------------------------------

def fame(views_de: int, views_en: int, weights: dict[str, float]) -> float:
    """V: weighted pageviews; a missing edition is 0, so no views at all is 0."""
    return weights["de"] * views_de + weights["en"] * views_en


# ---------------------------------------------------------------------------
# Spell helpers
# ---------------------------------------------------------------------------

def midpoint(earliest: date | None, latest: date | None) -> date | None:
    if earliest is None or latest is None:
        return None
    return earliest + (latest - earliest) / 2


def years_between(a: date, b: date) -> float:
    return (b - a).days / DAYS_PER_YEAR


def spell_bounds(s: Stint, ref_date: date, window_start: date | None = None) -> tuple[date, date] | None:
    """(mid_start, mid_end) or None when the spell has no usable dates (undated, or ends with no start).
    A current open-ended spell ends at ref_date; a weak one lasts a season; no end is after ref_date.
    With window_start, the start is clipped to it (never past the end); the end is not touched."""
    if s.date_quality == "undated" or "no_start" in s.weak_reasons:
        return None
    start = midpoint(s.earliest_start, s.latest_start)
    if start is None:
        return None
    if s.latest_end is None:
        end = start + timedelta(days=DAYS_PER_YEAR) if "open_ended" in s.weak_reasons else ref_date
    else:
        end = midpoint(s.earliest_end, s.latest_end)
    end = max(start, min(end, ref_date))
    if window_start is not None:
        start = min(max(start, window_start), end)
    return start, end


def spell_end(s: Stint, ref_date: date) -> date | None:
    """When the spell ended, for recency. Like spell_bounds, plus a no_start spell with a known end."""
    bounds = spell_bounds(s, ref_date)
    if bounds is not None:
        return bounds[1]
    if s.date_quality == "undated" or s.latest_end is None:
        return None
    end = midpoint(s.earliest_end, s.latest_end)
    return None if end is None else min(end, ref_date)


def tenure_years(s: Stint, p: FeatureParams, clip: bool = True) -> float:
    """Years the spell lasted, at least min_spell_seasons; clip counts only the part from window_start on."""
    bounds = spell_bounds(s, p.ref_date, p.window_start if clip else None)
    return p.min_spell_seasons if bounds is None else max(p.min_spell_seasons, years_between(*bounds))


def window_fraction(s: Stint, p: FeatureParams) -> float:
    """The share of the spell's tenure that falls inside the window; 1 when the dates are unusable."""
    full = spell_bounds(s, p.ref_date)
    if full is None or full[1] <= full[0]:
        return 1.0
    clipped = spell_bounds(s, p.ref_date, p.window_start)
    return years_between(*clipped) / years_between(*full)


def year_span(s: Stint) -> tuple[float, float]:
    """The spell's years, with the end as spell_bounds sees it: a weak open-ended spell lasts one season, a
    current one has no end."""
    start = _NEG if s.start_year is None else s.start_year
    if s.end_year is not None:
        return start, s.end_year
    if "open_ended" in s.weak_reasons and s.start_year is not None:
        return start, s.start_year + 1
    return start, _POS


def drop_duplicates(stints: list[Stint]) -> tuple[list[Stint], int]:
    """At one club, drops a spell that repeats another:
    - its year range lies inside another's and its apps are no higher (null counts as 0); identical spells with
      equal apps keep the lower fact id;
    - it overlaps an earlier-kept spell (lower fact id) and has the same non-null apps: the same spell recorded
      with different dates, e.g. by Wikidata and German Wikipedia.
    Spells that merely touch or do not overlap are all kept. Returns (kept, number dropped)."""
    def covered(s, o):
        (a0, a1), (b0, b1) = year_span(s), year_span(o)
        if not (b0 <= a0 and a1 <= b1) or (s.apps or 0) > (o.apps or 0):
            return False
        return (a0, a1) != (b0, b1) or (s.apps or 0) < (o.apps or 0) or s.fact_id > o.fact_id

    def same_spell(s, o):
        (a0, a1), (b0, b1) = year_span(s), year_span(o)
        return s.apps is not None and s.apps == o.apps and max(a0, b0) < min(a1, b1)

    kept = [s for s in stints if not any(covered(s, o) for o in stints if o is not s)]
    final: list[Stint] = []
    for s in sorted(kept, key=lambda x: x.fact_id):
        if not any(same_spell(s, o) for o in final):
            final.append(s)
    final_ids = {s.fact_id for s in final}
    return [s for s in kept if s.fact_id in final_ids], len(stints) - len(final)


def apps_at(stints: list[Stint], p: FeatureParams) -> tuple[float, bool, int]:
    """(apps, any imputed, duplicates dropped) over one club's player spells, counting the part of each spell
    inside the window: apps are pro-rated by the in-window fraction of the tenure. Null apps are imputed
    from the clipped tenure; zero apps stay zero."""
    kept, dropped = drop_duplicates(stints)
    total, imputed = 0.0, False
    for s in kept:
        if s.apps is None:
            total += p.imputed_apps_per_season * max(1.0, tenure_years(s, p))
            imputed = True
        else:
            total += s.apps * window_fraction(s, p)
    return total, imputed, dropped


def seasons_since_club(stints: list[Stint], p: FeatureParams) -> tuple[float, bool]:
    """(seasons since the latest spell end, unknown). Unknown falls back to the default."""
    ends = [e for s in stints if (e := spell_end(s, p.ref_date)) is not None]
    if not ends:
        return p.default_seasons_since, True
    return max(0.0, years_between(max(ends), p.ref_date)), False


def coach_tenure_years(stints: list[Stint], p: FeatureParams) -> float:
    """Years a person coached a club since the window start: the union of the spells, each at least min_spell_seasons long, so
    overlapping duplicate spells (Wikidata and German Wikipedia) count once. Spells with no usable dates
    count the floor once, and only when no spell has dates."""
    floor = timedelta(days=p.min_spell_seasons * DAYS_PER_YEAR)
    spans = sorted((b[0], max(b[1], b[0] + floor)) for s in stints
                   if (b := spell_bounds(s, p.ref_date, p.window_start)))
    if not spans:
        return p.min_spell_seasons if stints else 0.0
    total, (cur_start, cur_end) = timedelta(0), spans[0]
    for start, end in spans[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    return (total + (cur_end - cur_start)).days / DAYS_PER_YEAR


@dataclass
class SeasonSum:
    total: float = 0.0
    seasons: int = 0
    edge_seasons: int = 0
    last_season: int | None = None
    imputed: bool = False


def credited_rows(fact_id: int, ev: Evidence, title: bool = False) -> list[SeasonRow]:
    """The competition's season rows credited to a spell. stint_seasons credits a weak open-ended spell with every
    season up to now, but it probably lasted one: other templates keep only its first certain season (the first
    row if there is none). The title template keeps every row, because its answer set credits them all, but
    drops an edge row unless the spell starts and ends in the same year (the summer rule of
    club_title_winners_answers.sql: a year-only spell starts and ends between seasons)."""
    rows, s = ev.season_rows.get(fact_id, []), ev.stints.get(fact_id)
    if title:
        same_year = s is not None and s.start_year is not None and s.start_year == s.end_year
        return [r for r in rows if not r.edge or same_year]
    if s is not None and "open_ended" in s.weak_reasons and rows:
        first = min((r.season for r in rows if not r.edge), default=min(r.season for r in rows))
        rows = [r for r in rows if r.season == first]
    return rows


def usable_season_rows(fact_ids: list[int], ev: Evidence, title: bool = False) -> list[SeasonRow]:
    return [r for fid in fact_ids for r in credited_rows(fid, ev, title)]


def season_of(d: date) -> int:
    """The season a date falls in, counting a season from August (an approximation for seasons the data lacks)."""
    return d.year if d.month >= 8 else d.year - 1


def spell_season_weight(s: Stint, ev: Evidence, title: bool = False) -> float:
    """The total weight of the seasons the spell possibly covers: 1 for a season it certainly covers, and
    edge_season_weight for one it only possibly covers. Competition seasons use the stored rows; seasons the
    data lacks (another league's years) are worked out from the spell's bounds with August-to-May seasons.
    A weak open-ended spell counts only the season it lasts, except for the title template, which credits it with
    every season up to now."""
    p = ev.params
    if s.earliest_start is None:
        return 1.0
    weak_open = s.latest_end is None and "open_ended" in s.weak_reasons
    if s.latest_end is not None:
        end = s.latest_end
    elif weak_open and not title:
        end = s.earliest_start + timedelta(days=DAYS_PER_YEAR)
    else:
        end = p.ref_date
    end_season = season_of(end)
    weights = {r.season: (p.edge_season_weight if r.edge else 1.0) for r in ev.season_rows.get(s.fact_id, [])
               if r.season <= end_season}
    for y in range(season_of(s.earliest_start) - 1, end_season + 2):
        if y in weights:
            continue
        season_start, season_end = date(y, 8, 1), date(y + 1, 5, 31)
        if s.earliest_start <= season_end and end >= season_start:
            certain = (s.latest_start or s.earliest_start) <= season_end and (s.earliest_end or end) >= season_start
            weights[y] = 1.0 if certain else p.edge_season_weight
    return sum(weights.values()) or 1.0


def apps_units(fact_ids: list[int], ev: Evidence, title: bool = False) -> dict[int, tuple[float, bool]]:
    """fact id -> (apps per unit of season weight, imputed): the spell's apps spread over the seasons it possibly
    covers, so the seasons counted never add up to more than its apps. Null apps are imputed per season."""
    out = {}
    for fid in fact_ids:
        s = ev.stints.get(fid)
        if s is None:
            continue
        if s.apps is None:
            out[fid] = (ev.params.imputed_apps_per_season, True)
        else:
            out[fid] = (s.apps / spell_season_weight(s, ev, title), False)
    return out


def season_sum(rows: list[SeasonRow], ev: Evidence, value_of) -> SeasonSum:
    """Sums value_of(fact_id) -> (value, imputed) over distinct (club, season) pairs. A pair counts the
    largest value of the spells covering it, and edge_season_weight of that when every row is an edge."""
    pairs: dict[tuple[int, int], list[SeasonRow]] = {}
    for r in rows:
        pairs.setdefault((r.club_id, r.season), []).append(r)
    out = SeasonSum()
    for (_, season), group in sorted(pairs.items()):
        value, imputed = max((value_of(r.fact_id) for r in group), key=lambda v: v[0])
        edge = all(r.edge for r in group)
        out.total += (ev.params.edge_season_weight if edge else 1.0) * value
        out.seasons += 1
        out.edge_seasons += edge
        out.imputed = out.imputed or imputed
        out.last_season = season if out.last_season is None else max(out.last_season, season)
    return out


# ---------------------------------------------------------------------------
# Per-template features
# ---------------------------------------------------------------------------

def _fame_fields(entity_id: int, ev: Evidence) -> dict:
    de, en = ev.views.get(entity_id, (0, 0))
    return {"views_de": de, "views_en": en, "fame": fame(de, en, ev.fame_weights)}


def _stints_at(fact_ids: list[int], club_id: int, ev: Evidence) -> list[Stint]:
    return [s for f in fact_ids if (s := ev.stints.get(f)) is not None and s.club_id == club_id]


def _club_pair(entity_id, fact_ids, club_ids, ev):
    parts = []
    for club in club_ids:
        at = _stints_at(fact_ids, club, ev)
        apps, imputed, dropped = apps_at(at, ev.params)
        parts.append((apps, imputed, dropped, *seasons_since_club(at, ev.params)))
    return Features(
        **_fame_fields(entity_id, ev), tie_amount=min(p[0] for p in parts),
        seasons_since=min(p[3] for p in parts), apps_imputed=any(p[1] for p in parts),
        recency_unknown=any(p[4] for p in parts), duplicates_dropped=sum(p[2] for p in parts))


def _club_nationality(entity_id, fact_ids, club_ids, ev):
    at = _stints_at(fact_ids, club_ids[0], ev)
    apps, imputed, dropped = apps_at(at, ev.params)
    since, unknown = seasons_since_club(at, ev.params)
    return Features(**_fame_fields(entity_id, ev), tie_amount=apps, seasons_since=since, apps_imputed=imputed,
                    recency_unknown=unknown, duplicates_dropped=dropped)


def _club_coaches(entity_id, fact_ids, club_ids, ev):
    at = _stints_at(fact_ids, club_ids[0], ev)
    since, unknown = seasons_since_club(at, ev.params)
    return Features(**_fame_fields(entity_id, ev), tie_amount=coach_tenure_years(at, ev.params)
                    * ev.params.matches_per_season, seasons_since=since, recency_unknown=unknown)


def _from_season_sum(entity_id, total: SeasonSum, ev) -> Features:
    unknown = total.last_season is None
    since = ev.params.default_seasons_since if unknown else float(ev.params.reference_season - total.last_season)
    return Features(**_fame_fields(entity_id, ev), tie_amount=total.total, seasons_since=since,
                    apps_imputed=total.imputed, recency_unknown=unknown, seasons=total.seasons,
                    edge_seasons=total.edge_seasons)


def _competition_apps(entity_id, fact_ids, club_ids, ev):
    units = apps_units(fact_ids, ev)
    return _from_season_sum(entity_id, season_sum(usable_season_rows(fact_ids, ev), ev, units.__getitem__), ev)


def _title_apps(entity_id, fact_ids, club_ids, ev):
    titles = {e.season for f in fact_ids if (e := ev.events.get(f)) is not None
              and e.predicate == "won" and e.club_id == club_ids[0]}
    rows = [r for r in usable_season_rows(fact_ids, ev, title=True) if r.club_id == club_ids[0] and r.season in titles]
    units = apps_units(fact_ids, ev, title=True)
    return _from_season_sum(entity_id, season_sum(rows, ev, units.__getitem__), ev)


def _multi_club_coaches(entity_id, fact_ids, club_ids, ev):
    m = float(ev.params.matches_per_season)
    return _from_season_sum(entity_id, season_sum(usable_season_rows(fact_ids, ev), ev, lambda fid: (m, False)), ev)


def _club_events(entity_id, fact_ids, club_ids, ev):
    seasons = [e.season for f in fact_ids if (e := ev.events.get(f)) is not None
               and e.predicate == "competed_in" and e.season is not None]
    unknown = not seasons
    since = ev.params.default_seasons_since if unknown else float(ev.params.reference_season - max(seasons))
    return Features(**_fame_fields(entity_id, ev), tie_amount=float(len(seasons)), seasons_since=since,
                    recency_unknown=unknown)


TEMPLATE_FEATURES = {
    "club_pair": _club_pair,
    "club_nationality": _club_nationality,
    "club_coaches": _club_coaches,
    "bundesliga_nationality": _competition_apps,
    "club_title_winners": _title_apps,
    "multi_club_coaches": _multi_club_coaches,
    "relegated_clubs": _club_events,
    "promoted_clubs": _club_events,
}


def compute_features(answer_set: AnswerSet, ev: Evidence) -> dict[int, Features]:
    """Features for every answer of the set, in entity-id order."""
    fn = TEMPLATE_FEATURES[answer_set.template_id]
    return {e: fn(e, answer_set.answers[e], answer_set.club_ids, ev) for e in sorted(answer_set.answers)}
