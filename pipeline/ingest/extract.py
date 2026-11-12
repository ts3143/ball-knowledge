"""Turns Wikidata responses into entity, alias and fact rows, applying the step-1 scope rules.

Pure functions over parsed JSON: no network, no database. Every value comes from Wikidata,
except coach spells and player stints, which may also come from German Wikipedia infobox tables
(trainer_tabelle, vereine_tabelle, jugendvereine_tabelle; see dewiki.py). Every fact or value taken
from them is tagged dewiki (source, or a `dewiki*` qualifier on a Wikidata fact). Nothing here adds
or corrects a fact. What doesn't fit the rules is counted in `stats` for the coverage report instead.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from .dewiki import SENIOR_ROLES, CoachStation, PlayerRow
from .normalize import german_translit, normalize

# Wikidata properties and classes used by the scope rules.
P_INSTANCE_OF = "P31"
P_SEX = "P21"
P_BIRTH = "P569"
P_NATIONALITY = "P27"
P_POSITION = "P413"
P_FAMILY_NAME = "P734"
P_MEMBER_OF_TEAM = "P54"
P_HEAD_COACH = "P286"         # on the club item, value = person
P_COACH_OF = "P6087"          # on the person item, value = club
P_START = "P580"
P_END = "P582"
P_APPS = "P1350"
P_GOALS = "P1351"
P_SHORT_NAME = "P1813"
P_NICKNAME = "P1449"
Q_HUMAN = "Q5"
Q_FEMALE = "Q6581072"
Q_SQUAD = "Q103229495"        # "men's association football team" (season squad items)
P_POSITION_HELD = "P39"
# A coach statement (P6087 / P286) counts as a head-coach spell only if it has no P39 qualifier or
# every P39 value is here. Labels checked against the Wikidata API on 2026-11-11. Other values seen
# in the data (assistant coach Q11703711, youth coach Q114221078, scout Q1339677, goalkeeper coach
# Q2445218) are non-head roles.
Q_CARETAKER = "Q1050607"
HEAD_COACH_ROLES = {
    Q_CARETAKER,              # caretaker manager (de: Interimstrainer)
    "Q379533",                # player-coach (de: Spielertrainer), also in charge of the team
}
# classify_stint reasons for a kept-or-dropped stint whose dates don't prove the window.
UNPROVEN_REASONS = ("in_by_birth_year", "unproven_dates_born_before_cutoff", "unproven_dates_birth_unknown")


@dataclass(frozen=True)
class Scope:
    start_year: int
    min_birth_year: int
    languages: tuple[str, ...]


@dataclass(frozen=True)
class WdTime:
    date: date
    precision: int

    @property
    def year(self) -> int:
        return self.date.year


@dataclass
class EntityRow:
    wikidata_id: str
    type: str
    canonical_name: str
    description: str | None
    dewiki_title: str | None
    enwiki_title: str | None


@dataclass
class AliasRow:
    wikidata_id: str
    alias: str
    alias_norm: str
    kind: str
    lang: str | None


@dataclass
class FactRow:
    source_ref: str
    subject: str
    predicate: str
    object: str | None = None
    value_date: date | None = None
    start_year: int | None = None
    end_year: int | None = None
    start_date: date | None = None
    start_precision: int | None = None
    end_date: date | None = None
    end_precision: int | None = None
    date_quality: str | None = None
    apps: int | None = None
    goals: int | None = None
    qualifiers: dict = field(default_factory=dict)
    source: str = "wikidata"


@dataclass
class Season:
    qid: str
    start: WdTime
    end: WdTime | None
    teams: dict[str, str]  # listed team item -> canonical club


@dataclass
class Extraction:
    entities: dict[str, EntityRow] = field(default_factory=dict)
    aliases: dict[tuple[str, str], AliasRow] = field(default_factory=dict)
    facts: dict[str, FactRow] = field(default_factory=dict)
    stats: dict = field(default_factory=lambda: defaultdict(list))
    counts: Counter = field(default_factory=Counter)


# ---------------------------------------------------------------------------
# Wikidata value parsing
# ---------------------------------------------------------------------------

_TIME = re.compile(r"^([+-])(\d+)-(\d\d)-(\d\d)T")


def parse_time_string(value: str, precision: int) -> WdTime | None:
    """Wikidata time (+2005-07-01T00:00:00Z) at year precision or finer; coarser is unknown."""
    if precision < 9:
        return None
    m = _TIME.match(value)
    if not m or m.group(1) == "-":
        return None
    year, month, day = int(m.group(2)), int(m.group(3)), int(m.group(4))
    if not 1 <= year <= 9999:
        # A typo in the data (e.g. 20009). Treated as unknown and listed by unreadable_dates().
        return None
    if precision == 9:
        month = day = 1
    elif precision == 10:
        day = 1
    try:
        return WdTime(date(year, max(month, 1), max(day, 1)), min(precision, 11))
    except ValueError:
        # Impossible calendar date in the data: keep the year only.
        return WdTime(date(year, 1, 1), 9)


def unreadable_dates(statement: dict, pids: tuple[str, ...]) -> list[str]:
    """Raw values of date qualifiers that hold a value at year precision or finer but don't parse."""
    bad = []
    for pid in pids:
        snak = qualifier(statement, pid)
        if snak and snak.get("snaktype") == "value":
            v = snak["datavalue"]["value"]
            if v["precision"] >= 9 and parse_time_string(v["time"], v["precision"]) is None:
                bad.append(f"{pid}={v['time']}")
    return bad


def snak_time(snak: dict) -> WdTime | None:
    if snak.get("snaktype") != "value":
        return None
    v = snak["datavalue"]["value"]
    return parse_time_string(v["time"], v["precision"])


def snak_item(snak: dict) -> str | None:
    if snak.get("snaktype") != "value" or snak["datavalue"]["type"] != "wikibase-entityid":
        return None
    return snak["datavalue"]["value"].get("id")


def snak_int(snak: dict) -> int | None:
    if snak.get("snaktype") != "value" or snak["datavalue"]["type"] != "quantity":
        return None
    try:
        return int(float(snak["datavalue"]["value"]["amount"]))
    except (KeyError, ValueError):
        return None


def snak_simple(snak: dict):
    """A JSON-friendly rendering of any snak, for the raw `qualifiers` column."""
    if snak.get("snaktype") != "value":
        return snak.get("snaktype")
    dv = snak["datavalue"]
    v = dv["value"]
    match dv["type"]:
        case "wikibase-entityid":
            return v.get("id")
        case "time":
            return {"time": v["time"], "precision": v["precision"]}
        case "quantity":
            return v["amount"]
        case "monolingualtext":
            return {"text": v["text"], "lang": v["language"]}
        case _:
            return v


def statements(entity: dict, pid: str) -> list[dict]:
    """Non-deprecated statements for a property."""
    return [s for s in entity.get("claims", {}).get(pid, []) if s.get("rank") != "deprecated"]


def deprecated_count(entity: dict, pid: str) -> int:
    return sum(1 for s in entity.get("claims", {}).get(pid, []) if s.get("rank") == "deprecated")


def qualifier(statement: dict, pid: str) -> dict | None:
    snaks = statement.get("qualifiers", {}).get(pid)
    return snaks[0] if snaks else None


def qualifier_time(statement: dict, pid: str) -> WdTime | None:
    snak = qualifier(statement, pid)
    return snak_time(snak) if snak else None


def qualifier_int(statement: dict, pid: str) -> int | None:
    snak = qualifier(statement, pid)
    return snak_int(snak) if snak else None


def all_qualifiers(statement: dict) -> dict:
    return {pid: [snak_simple(s) for s in snaks] for pid, snaks in statement.get("qualifiers", {}).items()}


def item_values(entity: dict, pid: str) -> list[str]:
    return [q for s in statements(entity, pid) if (q := snak_item(s["mainsnak"]))]


def position_held(statement: dict) -> list[str]:
    return [q for snak in statement.get("qualifiers", {}).get(P_POSITION_HELD, []) if (q := snak_item(snak))]


def best_statement(entity: dict, pid: str) -> dict | None:
    stmts = statements(entity, pid)
    preferred = [s for s in stmts if s.get("rank") == "preferred"]
    return (preferred or stmts or [None])[0]


def birth(entity: dict) -> tuple[WdTime | None, dict | None]:
    st = best_statement(entity, P_BIRTH)
    return (snak_time(st["mainsnak"]) if st else None), st


def label(entity: dict, languages: tuple[str, ...]) -> str | None:
    labels = entity.get("labels", {})
    for lang in languages:
        if lang in labels:
            return labels[lang]["value"]
    return None


def description(entity: dict) -> str | None:
    descs = entity.get("descriptions", {})
    for lang in ("en", "de"):
        if lang in descs:
            return descs[lang]["value"]
    return None


def sitelink(entity: dict, site: str) -> str | None:
    link = entity.get("sitelinks", {}).get(site)
    return link["title"] if link else None


# ---------------------------------------------------------------------------
# Scope rules
# ---------------------------------------------------------------------------

def classify_stint(start: WdTime | None, end: WdTime | None, birth_year: int | None,
                   scope: Scope) -> tuple[str | None, str]:
    """(date_quality, reason). date_quality None means the stint is out of scope.

    In scope if it ends in or after scope.start_year, or starts then with no end. Stints whose
    dates don't prove that (no dates, or an earlier start with no end) are kept, flagged, only
    for people born in scope.min_birth_year or later.
    """
    if end is not None:
        if end.year >= scope.start_year:
            return "dated", "in"
        return None, "ended_before_window"
    quality = "open_ended" if start is not None else "undated"
    if start is not None and start.year >= scope.start_year:
        return quality, "in"
    if birth_year is not None and birth_year >= scope.min_birth_year:
        return quality, "in_by_birth_year"
    if birth_year is None:
        return None, "unproven_dates_birth_unknown"
    return None, "unproven_dates_born_before_cutoff"


def stint_fact(statement: dict, subject: str, predicate: str, club: str, quality: str,
               via_item: str | None) -> FactRow:
    start = qualifier_time(statement, P_START)
    end = qualifier_time(statement, P_END)
    quals = all_qualifiers(statement)
    if via_item:
        quals["via_item"] = via_item
    return FactRow(
        source_ref=statement["id"],
        subject=subject,
        predicate=predicate,
        object=club,
        start_year=start.year if start else None,
        end_year=end.year if end else None,
        start_date=start.date if start else None,
        start_precision=start.precision if start else None,
        end_date=end.date if end else None,
        end_precision=end.precision if end else None,
        date_quality=quality,
        apps=qualifier_int(statement, P_APPS),
        goals=qualifier_int(statement, P_GOALS),
        qualifiers=quals,
    )


# ---------------------------------------------------------------------------
# Seasons
# ---------------------------------------------------------------------------

def parse_seasons(rows: list[dict[str, str]], qid) -> tuple[dict[str, Season], dict[str, str]]:
    """SPARQL season rows -> seasons, and the squad item -> club map."""
    seasons: dict[str, Season] = {}
    squad_to_club: dict[str, str] = {}
    for r in rows:
        sq = qid(r["season"])
        if sq not in seasons:
            start = parse_time_string("+" + r["start"], 11)
            end = parse_time_string("+" + r["end"], 11) if r.get("end") else None
            seasons[sq] = Season(sq, start, end, {})
        if "team" in r:
            team = qid(r["team"])
            club = qid(r["parent"]) if "parent" in r else team
            seasons[sq].teams[team] = club
            if club != team:
                squad_to_club[team] = club
    return seasons, squad_to_club


def season_facts(seasons: dict[str, Season], winners: list[dict[str, str]], competition: str,
                 squad_to_club: dict[str, str], qid, out: Extraction) -> None:
    clubs = {c for s in seasons.values() for c in s.teams.values()}
    for s in seasons.values():
        for team, club in s.teams.items():
            ref = f"season:{s.qid}:club:{club}"
            if ref in out.facts:
                out.counts["season_club_listed_twice"] += 1
                continue
            out.facts[ref] = FactRow(
                source_ref=ref, subject=club, predicate="competed_in", object=competition,
                start_year=s.start.year, end_year=s.end.year if s.end else None,
                start_date=s.start.date, start_precision=11,
                end_date=s.end.date if s.end else None, end_precision=11 if s.end else None,
                date_quality="dated", qualifiers={"season": s.qid, "listed_as": team},
            )
    won: dict[str, list[str]] = defaultdict(list)
    for r in winners:
        sq, winner = qid(r["season"]), qid(r["winner"])
        club = squad_to_club.get(winner, winner)
        if sq not in seasons or club not in clubs or "criterion" in r:
            out.stats["winners_not_a_club"].append((sq, winner, qid(r["criterion"]) if "criterion" in r else None))
            continue
        won[sq].append(club)
        s = seasons[sq]
        ref = f"season:{sq}:club:{club}:won"
        out.facts[ref] = FactRow(
            source_ref=ref, subject=club, predicate="won", object=competition,
            start_year=s.start.year, end_year=s.end.year if s.end else None,
            start_date=s.start.date, start_precision=11,
            end_date=s.end.date if s.end else None, end_precision=11 if s.end else None,
            date_quality="dated", qualifiers={"season": sq, "listed_as": winner},
        )
    for s in sorted(seasons.values(), key=lambda s: s.start.date):
        out.stats["seasons"].append({
            "qid": s.qid, "start_year": s.start.year, "end_year": s.end.year if s.end else None,
            "teams": len(set(s.teams.values())), "squad_items": sum(1 for t, c in s.teams.items() if t != c),
            "winners": won.get(s.qid, []),
        })


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

def people_facts(persons: dict[str, dict], clubs: dict[str, dict], squad_to_club: dict[str, str],
                 club_ids: set[str], scope: Scope, out: Extraction,
                 stations: list[CoachStation] = (), player_rows: list[PlayerRow] = (),
                 unreadable_rows: set = frozenset()) -> set[str]:
    """Stint, coach and attribute facts for people in scope. Returns the in-scope person ids.

    `stations` are infobox coaching rows at our clubs. A Wikidata coach link whose own dates
    don't prove the window is settled by the person's rows at that club when there are any,
    instead of by the birth-year rule. `player_rows` are the infobox career rows at our clubs;
    they settle Wikidata player links the same way and reconcile the others (_dewiki_player_facts).
    `unreadable_rows` are the (person, club) pairs with a senior row we couldn't read: nothing of that
    person at that club is dropped because of the rows we could read."""
    target_to_club = {c: c for c in club_ids} | squad_to_club
    candidate: dict[str, list[FactRow]] = defaultdict(list)
    reasons_by_person: dict[str, Counter] = defaultdict(Counter)
    birth_years: dict[str, int | None] = {}
    station_pairs = {(s.person, s.club) for s in stations}
    deferred: dict[tuple[str, str], list[str]] = defaultdict(list)
    row_pairs = {(r.person, r.club) for r in player_rows}
    unproven: dict[str, str] = {}                       # played_for statement id -> classify_stint reason
    out_of_window: dict[str, dict[str, list[tuple[int | None, int | None]]]] = defaultdict(lambda: defaultdict(list))

    for pid, person in persons.items():
        if person["id"] != pid:
            out.stats["redirected_people"].append((pid, person["id"]))
            continue
        if Q_HUMAN not in item_values(person, P_INSTANCE_OF):
            out.counts["not_human"] += 1
            continue
        b, _ = birth(person)
        birth_years[pid] = b.year if b else None

    def consider(person_id: str, statement: dict, predicate: str, target: str) -> None:
        club = target_to_club[target]
        roles = position_held(statement) if predicate != "played_for" else []
        if any(q not in HEAD_COACH_ROLES for q in roles):
            # Rejected before anything else: no fact, no scope, no merging, no infobox reconciliation.
            out.counts[f"{predicate}:non_head_role"] += 1
            out.stats["coach_statements_non_head_role"].append((person_id, club, statement["id"], roles))
            return
        start = qualifier_time(statement, P_START)
        end = qualifier_time(statement, P_END)
        if bad := unreadable_dates(statement, (P_START, P_END)):
            out.stats["unreadable_dates"].append((person_id, club, statement["id"], bad))
        quality, reason = classify_stint(start, end, birth_years[person_id], scope)
        if (predicate != "played_for" and reason not in ("in", "ended_before_window")
                and (person_id, club) in station_pairs):
            reason = "deferred_to_dewiki"
            deferred[(person_id, club)].append(statement["id"])
            quality = None
        if predicate == "played_for" and reason == "ended_before_window":
            out_of_window[person_id][club].append((start.year if start else None, end.year if end else None))
        if predicate == "played_for" and reason in UNPROVEN_REASONS and (person_id, club) in unreadable_rows:
            # The old rule applies: kept when born in scope.min_birth_year or later, dropped otherwise.
            out.counts["players_unproven_link_unreadable_row_guard:"
                       + ("kept_old_rule" if reason == "in_by_birth_year" else "dropped_old_rule")] += 1
        elif predicate == "played_for" and reason in UNPROVEN_REASONS and (person_id, club) in row_pairs:
            # Kept for now, flagged: the person's career rows at this club date it, replace it or drop it.
            unproven[statement["id"]] = reason
            reason = "deferred_to_dewiki"
            quality = "open_ended" if start is not None else "undated"
        out.counts[f"{predicate}:{reason}"] += 1
        reasons_by_person[person_id][reason] += 1
        if quality:
            via = target if target != club else None
            if via:
                out.counts[f"{predicate}:via_squad_item"] += 1
            fact = stint_fact(statement, person_id, predicate, club, quality, via)
            if Q_CARETAKER in roles:
                fact.qualifiers["role"] = "interim"
            candidate[person_id].append(fact)

    # Player stints and P6087 coach links live on the person item.
    for pid in birth_years:
        person = persons[pid]
        out.counts["deprecated_skipped"] += deprecated_count(person, P_MEMBER_OF_TEAM) + deprecated_count(person, P_COACH_OF)
        for st in statements(person, P_MEMBER_OF_TEAM):
            target = snak_item(st["mainsnak"])
            if target in target_to_club:
                consider(pid, st, "played_for", target)
        for st in statements(person, P_COACH_OF):
            target = snak_item(st["mainsnak"])
            if target in target_to_club:
                consider(pid, st, "coached_p6087", target)

    # P286 head-coach statements live on the club (and squad) items.
    for cid, club in clubs.items():
        if cid not in target_to_club:
            continue
        out.counts["deprecated_skipped"] += deprecated_count(club, P_HEAD_COACH)
        for st in statements(club, P_HEAD_COACH):
            person_id = snak_item(st["mainsnak"])
            if person_id in birth_years:
                consider(person_id, st, "coached_p286", cid)

    stations_by_person: dict[str, list[CoachStation]] = defaultdict(list)
    for s in stations:
        if s.person in birth_years:
            stations_by_person[s.person].append(s)
        else:
            out.counts["dewiki_rows_person_not_human_or_redirected"] += 1
    rows_by_person: dict[str, list[PlayerRow]] = defaultdict(list)
    for r in player_rows:
        if r.person in birth_years:
            rows_by_person[r.person].append(r)
        else:
            out.counts["dewiki_player_rows_person_not_human_or_redirected"] += 1

    in_scope: set[str] = set()
    kept_people: set[str] = set()
    for pid in sorted(set(candidate) | set(stations_by_person) | set(rows_by_person)):
        facts = _merge_coaches(candidate.get(pid, []), out)
        facts += _dewiki_coach_facts(pid, facts, stations_by_person.get(pid, []), deferred, scope, out)
        facts += _dewiki_player_facts(pid, facts, unproven, rows_by_person.get(pid, []),
                                      out_of_window.get(pid, {}), scope, out, unreadable_rows)
        if not facts:
            continue
        kept_people.add(pid)
        if Q_FEMALE in item_values(persons[pid], P_SEX):
            out.counts["women_on_mens_club_items"] += 1
            out.stats["women_excluded"].append(pid)
            continue
        in_scope.add(pid)
        for f in facts:
            out.facts[f.source_ref] = f
    for pid, reasons in reasons_by_person.items():
        if pid not in kept_people:
            out.counts["people_dropped"] += 1
            key = ("people_dropped_born_before_cutoff" if reasons["unproven_dates_born_before_cutoff"] else
                   "people_dropped_birth_unknown" if reasons["unproven_dates_birth_unknown"] else
                   "people_dropped_by_dewiki_rows" if reasons["deferred_to_dewiki"] else
                   "people_dropped_ended_before_window")
            out.counts[key] += 1
    out.counts["people_found_no_link"] = len(birth_years) - len(
        reasons_by_person.keys() | stations_by_person.keys() | rows_by_person.keys())
    out.counts["people_only_from_dewiki"] = len(in_scope - reasons_by_person.keys())

    for pid in sorted(in_scope):
        person = persons[pid]
        b, b_st = birth(person)
        if b and b_st:
            out.facts[b_st["id"]] = FactRow(source_ref=b_st["id"], subject=pid, predicate="born",
                                            value_date=b.date, qualifiers={"precision": b.precision})
        for pred, prop in (("nationality", P_NATIONALITY), ("position", P_POSITION)):
            for st in statements(person, prop):
                obj = snak_item(st["mainsnak"])
                if obj:
                    out.facts[st["id"]] = FactRow(source_ref=st["id"], subject=pid, predicate=pred, object=obj,
                                                  qualifiers=all_qualifiers(st))
    return in_scope


def _station_in_window(s: CoachStation, scope: Scope) -> bool:
    return s.end >= scope.start_year if s.end is not None else s.start >= scope.start_year


def _station_overlaps(f: FactRow, s: CoachStation) -> bool:
    return (f.start_year or 0) <= (s.end or 9999) and s.start <= (f.end_year or 9999)


def _dewiki_coach_facts(person: str, kept: list[FactRow], stations: list[CoachStation],
                        deferred: dict[tuple[str, str], list[str]], scope: Scope,
                        out: Extraction) -> list[FactRow]:
    """Reconciles one person's infobox coaching rows with their Wikidata coach spells.

    - A head or interim row overlapping a kept Wikidata spell at the same club confirms it. The
      row goes into the spell's qualifiers and Wikidata's dates stay; differing years are reported.
    - Any other head or interim row in the window becomes a fact of its own (source dewiki). If
      Wikidata links the person to that club without usable dates, the link ids are kept on it.
    - A deferred Wikidata link that no such row matches is dropped, with the infobox's reason:
      every head-coach row is before the window, or there are only assistant/youth/reserve rows.
    - A kept Wikidata spell that only assistant/youth/reserve rows overlap is removed from `kept`
      (decided by T, 2026-11-11): Wikidata's "coach of" doesn't distinguish assistants.
    """
    new: list[FactRow] = []
    taken_refs: set[str] = set()
    wd_spells = [f for f in kept if f.predicate == "coached"]
    for club in sorted({s.club for s in stations}):
        here = [s for s in stations if s.club == club]
        heads = [s for s in here if s.role != "other"]
        in_window = [s for s in heads if _station_in_window(s, scope)]
        links = deferred.get((person, club), [])
        for f in wd_spells:
            # Rule 2: a Wikidata spell with a start but no end takes the end year of the row that
            # starts in the same year (or within one).
            if f.object != club or f.start_year is None or f.end_year is not None:
                continue
            near = [s for s in in_window if abs(s.start - f.start_year) <= 1]
            if any(s.end is None for s in near):
                continue        # an open row at this club confirms the spell as current: never close it
            match = sorted((s for s in near if s.end >= f.start_year),
                           key=lambda s: (abs(s.start - f.start_year), s.start))
            if match:
                s = match[0]
                f.end_year, f.date_quality = s.end, "dated"
                f.qualifiers["dewiki_filled"] = {"end_year": s.end, "page": s.page, "rev": s.rev_id, "row": s.row,
                                                 "role": s.role}
                out.counts["coach_end_year_filled"] += 1
                out.stats["coach_end_year_filled"].append((person, club, f.source_ref, s.end, s.row))
        for s in in_window:
            twin = next((f for f in wd_spells if f.object == club and _station_overlaps(f, s)), None)
            if twin is not None:
                twin.qualifiers.setdefault("dewiki", []).append(
                    {"page": s.page, "rev": s.rev_id, "row": s.row, "role": s.role})
                out.counts["dewiki_rows_confirming_wikidata"] += 1
                if (twin.start_year, twin.end_year) != (s.start, s.end):
                    out.stats["coach_dewiki_disagreements"].append(
                        (person, club, [twin.start_year, twin.end_year], [s.start, s.end], s.role))
                continue
            quals = {"source_property": "dewiki_infobox", "role": s.role, "dewiki_page": s.page,
                     "dewiki_rev": s.rev_id, "row": s.row}
            if links:
                quals["wikidata_links"] = links
            out.counts["dewiki_rows_dating_wikidata_link" if links else "dewiki_rows_only_source"] += 1
            # Keyed on page, club, start and role only: closing a spell later keeps the same fact.
            ref = f"dewiki:{s.page_id}:coached:{club}:{s.start}:{s.role}"
            while ref in taken_refs:
                ref += "+"
            taken_refs.add(ref)
            new.append(FactRow(
                source_ref=ref,
                subject=person, predicate="coached", object=club, start_year=s.start, end_year=s.end,
                date_quality="dated" if s.end is not None else "open_ended", qualifiers=quals, source="dewiki"))
        if links:
            outcome = ("dated_by_dewiki" if in_window else "dewiki_rows_before_window" if heads
                       else "dewiki_rows_not_head_coach")
            out.counts[f"coach_links:{outcome}"] += len(links)
            if not in_window:
                out.stats[f"coach_links_{outcome}"].append((here[0].page, club, [s.row for s in here]))
        for f in wd_spells:
            if f.object == club and not any(_station_overlaps(f, s) for s in heads) \
                    and any(_station_overlaps(f, s) for s in here):
                kept.remove(f)
                out.stats["coach_spells_dewiki_says_other_role"].append(
                    (person, club, [f.start_year, f.end_year], [s.row for s in here if _station_overlaps(f, s)]))
    return new


def _row_ref(r: PlayerRow) -> dict:
    return {"page": r.page, "rev": r.rev_id, "row": r.row, "role": r.role}


def _row_in_window(r: PlayerRow, scope: Scope) -> bool:
    return r.end >= scope.start_year if r.end is not None else r.start >= scope.start_year


def _row_overlaps(start: int | None, end: int | None, r: PlayerRow) -> bool:
    return (start or 0) <= (r.end if r.end is not None else 9999) and r.start <= (end if end is not None else 9999)


def _dewiki_player_facts(person: str, kept: list[FactRow], unproven: dict[str, str], rows: list[PlayerRow],
                         out_of_window: dict[str, list[tuple[int | None, int | None]]], scope: Scope,
                         out: Extraction, unreadable: set = frozenset()) -> list[FactRow]:
    """Reconciles one person's infobox career rows with their Wikidata played_for stints, club by club.

    Rows are first_team and loan ("senior": playing for the club), reserve and youth. Wikidata's years
    win wherever both have them. `unproven` holds the stints whose own dates don't prove the window
    (kept only because the person has rows at that club). Counted as players_rule1 ... players_rule7:

    1. An unproven Wikidata link that no row of the same start year confirms is replaced by the
       senior rows in the window (source dewiki, the link ids kept on them).
    2. A stint with a start but no end takes the end year of the senior row that starts in the same
       year (or within one), tagged `dewiki_filled`; an open row confirms the spell as current.
    3. A stint without apps takes the apps (and goals) of the one senior row overlapping it.
    4. A stint that only reserve and/or youth rows overlap is dropped.
    5. A senior row in the window that no Wikidata stint at the club overlaps becomes a fact.
    6. A stint overlapping a senior row with different years keeps its years and the row in qualifiers.
    7. No rows at the club: unchanged.

    A person with a senior row at the club that couldn't be read (`unreadable`) never loses a stint
    there to rule 4.
    """
    new: list[FactRow] = []
    taken: set[str] = set()
    wd_all = [f for f in kept if f.predicate == "played_for"]
    for club in sorted({r.club for r in rows} | {f.object for f in wd_all}):
        wd = [f for f in wd_all if f.object == club]
        here = [r for r in rows if r.club == club]
        if not here:
            out.counts["players_rule7_unchanged_no_dewiki_rows"] += len(wd)
            continue
        senior = [r for r in here if r.role in SENIOR_ROLES]

        # Rule 2: end years.
        confirmed: set[str] = set()
        for f in wd:
            if f.start_year is None or f.end_year is not None:
                continue
            near = [r for r in senior if abs(r.start - f.start_year) <= 1]
            open_rows = [r for r in near if r.end is None]
            if open_rows:
                # An open current row confirms the spell as current: never close it with a loan's end.
                f.qualifiers.setdefault("dewiki", []).append(_row_ref(open_rows[0]))
                confirmed.add(f.source_ref)
                out.counts["players_rule2_open_spell_confirmed_current"] += 1
                continue
            ended = sorted((r for r in near if r.end >= f.start_year),
                           key=lambda r: (abs(r.start - f.start_year), r.start))
            if ended:
                r = ended[0]
                f.end_year, f.date_quality = r.end, "dated"
                f.qualifiers["dewiki_filled"] = {"end_year": r.end, **_row_ref(r)}
                out.counts["players_rule2_end_year_filled"] += 1
                out.stats["players_rule2_end_year_filled"].append((person, club, f.source_ref, r.end, r.row))

        # Unproven links: resolved by the fill above, replaced by rows (rule 1), or dropped.
        links: list[str] = []
        for f in list(wd):
            if f.source_ref not in unproven:
                continue
            if f.source_ref in confirmed or (f.end_year is not None and f.end_year >= scope.start_year
                                              and "dewiki_filled" in f.qualifiers):
                out.counts["players_unproven_link_proven_by_dewiki_row"] += 1
            elif f.end_year is not None and "dewiki_filled" in f.qualifiers:
                out.counts["players_unproven_link_ended_before_window"] += 1
                out.stats["players_unproven_link_ended_before_window"].append((person, club, f.source_ref))
                kept.remove(f)
                wd.remove(f)
            else:
                links.append(f.source_ref)
                kept.remove(f)
                wd.remove(f)

        # Rules 3, 4, 6 on the remaining stints.
        for f in list(wd):
            over = [r for r in senior if _row_overlaps(f.start_year, f.end_year, r)]
            if over:
                if f.apps is None:
                    with_apps = [r for r in over if r.apps is not None]
                    if len(over) == 1 and with_apps:
                        f.apps = with_apps[0].apps
                        filled = {"apps": f.apps}
                        if f.goals is None and with_apps[0].goals is not None:
                            f.goals = with_apps[0].goals
                            filled["goals"] = f.goals
                        f.qualifiers["dewiki_apps_filled"] = {**filled, **_row_ref(with_apps[0])}
                        out.counts["players_rule3_apps_filled"] += 1
                    elif len(over) > 1 and with_apps:
                        out.counts["players_rule3_skipped_several_rows"] += 1
                        out.stats["players_rule3_skipped_several_rows"].append(
                            (person, club, f.source_ref, [r.row for r in over]))
                f.qualifiers.setdefault("dewiki", []).extend(
                    _row_ref(r) for r in over if _row_ref(r) not in f.qualifiers.get("dewiki", []))
                if not any((r.start, r.end) == (f.start_year, f.end_year) for r in over):
                    out.counts["players_rule6_years_disagree"] += 1
                    out.stats["players_rule6_years_disagree"].append(
                        (person, club, [f.start_year, f.end_year], [r.row for r in over]))
            elif any(_row_overlaps(f.start_year, f.end_year, r) for r in here) and (person, club) in unreadable:
                out.counts["players_rule4_kept_unreadable_senior_row"] += 1
                out.stats["players_rule4_kept_unreadable_senior_row"].append((person, club, f.source_ref))
            elif any(_row_overlaps(f.start_year, f.end_year, r) for r in here):
                out.counts["players_rule4_stint_dropped_reserve_youth_only"] += 1
                out.stats["players_rule4_stint_dropped_reserve_youth_only"].append(
                    (person, club, f.source_ref, [f.start_year, f.end_year], f.date_quality,
                     [r.row for r in here if _row_overlaps(f.start_year, f.end_year, r)]))
                kept.remove(f)
                wd.remove(f)
            else:
                out.counts["players_rule7_unchanged_no_overlapping_row"] += 1

        # Rules 1 and 5: senior rows in the window that no remaining stint overlaps.
        spans = [(f.start_year, f.end_year) for f in wd] + out_of_window.get(club, [])
        added = 0
        for r in sorted(senior, key=lambda r: (r.start, r.role != "loan", r.end or 9999)):
            if not _row_in_window(r, scope):
                continue
            if any(_row_overlaps(a, b, r) for a, b in spans):
                if not any(_row_overlaps(f.start_year, f.end_year, r) for f in wd):
                    out.counts["players_rule5_blocked_by_wikidata_stint_before_window"] += 1
                    out.stats["players_rule5_blocked_by_wikidata_stint_before_window"].append(
                        (person, club, r.row, out_of_window.get(club)))
                continue
            # Keyed on page, club and start only: closing a current row or changing its loan marker
            # keeps the same fact (end, apps and role are updatable columns); a genuine collision
            # (a loan and a permanent row starting the same year) gets a suffix, the loan first.
            ref = f"dewiki:{r.page_id}:played_for:{club}:{r.start}"
            while ref in taken:
                ref += "+"
            taken.add(ref)
            quals = {"source_property": "dewiki_infobox_player", "role": r.role, "dewiki_page": r.page,
                     "dewiki_rev": r.rev_id, "row": r.row}
            if links:
                quals["wikidata_links"] = links
            key = "players_rule1_rows_replacing_unproven_link" if links else "players_rule5_rows_only_source"
            out.counts[key] += 1
            out.stats[key].append((person, club, r.row, links))
            added += 1
            new.append(FactRow(source_ref=ref, subject=person, predicate="played_for", object=club,
                               start_year=r.start, end_year=r.end, apps=r.apps, goals=r.goals,
                               date_quality="dated" if r.end is not None else "open_ended",
                               qualifiers=quals, source="dewiki"))
        if links:
            out.counts["players_rule1_links_replaced" if added else "players_unproven_links_dropped"] += len(links)
            if not added:
                in_window = [r for r in senior if _row_in_window(r, scope)]
                why = ("reserve_youth_rows_only" if not senior else
                       "senior_rows_before_window" if not in_window else "senior_rows_covered_by_wikidata_stint")
                out.counts[f"players_unproven_links_dropped:{why}"] += len(links)
                out.stats[f"players_unproven_links_dropped_{why}"].append(
                    (person, club, links, [r.row for r in here]))
    return new


def _spells_overlap(a: FactRow, b: FactRow) -> bool:
    """True when two coach entries could be the same spell. An undated entry can't be told
    apart from any dated spell at the same club, so it overlaps everything."""
    if (a.start_year is None and a.end_year is None) or (b.start_year is None and b.end_year is None):
        return True
    return (a.start_year or 0) <= (b.end_year or 9999) and (b.start_year or 0) <= (a.end_year or 9999)


def _merge_coaches(facts: list[FactRow], out: Extraction) -> list[FactRow]:
    """Collapses each P6087 coach entry into an overlapping P286 entry at the same club, keeping
    P286 (it has day-precision dates). P6087 entries with no P286 counterpart are kept."""
    players = [f for f in facts if f.predicate == "played_for"]
    p286 = sorted((f for f in facts if f.predicate == "coached_p286"), key=lambda f: (f.start_year or 0, f.source_ref))
    p6087 = [f for f in facts if f.predicate == "coached_p6087"]
    kept = list(p286)
    for f in p6087:
        twin = next((t for t in p286 if t.object == f.object and _spells_overlap(t, f)), None)
        if twin is None:
            out.counts["coach_spells_only_in_p6087"] += 1
            kept.append(f)
            continue
        twin.qualifiers.setdefault("also_p6087", []).append(f.source_ref)
        out.counts["coach_entries_collapsed"] += 1
        if f.start_year is not None and (f.start_year, f.end_year) != (twin.start_year, twin.end_year):
            out.stats["coach_date_disagreements"].append(
                (f.subject, f.object, [twin.start_year, twin.end_year], [f.start_year, f.end_year]))
    for f in kept:
        f.qualifiers["source_property"] = P_HEAD_COACH if f.predicate == "coached_p286" else P_COACH_OF
        f.predicate = "coached"
    return players + kept


# ---------------------------------------------------------------------------
# Entities and aliases
# ---------------------------------------------------------------------------

def add_entity(out: Extraction, entity: dict, etype: str, scope: Scope) -> None:
    qid = entity["id"]
    name = label(entity, scope.languages)
    if name is None:
        # Only labels in other languages (often other scripts). Use one as-is; never transliterate.
        others = sorted(entity.get("labels", {}).items())
        name = others[0][1]["value"] if others else qid
        out.stats["no_label"].append((qid, others[0][0] if others else None, name))
    out.entities[qid] = EntityRow(qid, etype, name, description(entity),
                                  sitelink(entity, "dewiki"), sitelink(entity, "enwiki"))


def add_aliases(out: Extraction, owner: str, entity: dict, scope: Scope,
                family_names: dict[str, dict] | None = None) -> None:
    """Labels, family names, alt labels, short names and nicknames, in that priority order
    (a name found twice keeps the first kind)."""
    found: list[tuple[str, str, str | None]] = []
    for lang in scope.languages:
        if lang in entity.get("labels", {}):
            found.append((entity["labels"][lang]["value"], "label", lang))
    if family_names is not None:
        for fam in item_values(entity, P_FAMILY_NAME):
            fam_entity = family_names.get(fam)
            for lang in scope.languages:
                if fam_entity and lang in fam_entity.get("labels", {}):
                    found.append((fam_entity["labels"][lang]["value"], "family_name", lang))
    for lang in scope.languages:
        for a in entity.get("aliases", {}).get(lang, []):
            found.append((a["value"], "alt_label", lang))
    for prop, kind in ((P_SHORT_NAME, "short_name"), (P_NICKNAME, "nickname")):
        for st in statements(entity, prop):
            snak = st["mainsnak"]
            if snak.get("snaktype") == "value" and snak["datavalue"]["type"] == "monolingualtext":
                v = snak["datavalue"]["value"]
                if v["language"] in scope.languages:
                    found.append((v["text"], kind, v["language"]))

    for text, kind, lang in found:
        _put_alias(out, owner, text, normalize(text), kind, lang)
    for text, _, lang in found:
        translit = german_translit(text)
        if translit:
            _put_alias(out, owner, text, translit, "translit", lang)


def _put_alias(out: Extraction, owner: str, text: str, norm: str, kind: str, lang: str | None) -> None:
    if not norm or (owner, norm) in out.aliases:
        return
    out.aliases[(owner, norm)] = AliasRow(owner, text, norm, kind, lang)
