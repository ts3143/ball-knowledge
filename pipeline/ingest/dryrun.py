"""--dry-run: load inside a transaction that is always rolled back, and diff the facts table.

The numbers are exactly what a real run would do, because the real `load()` runs. The full detail
goes to pipeline/data/cache/dewiki_players_dryrun.json.
"""

import json
import random
import re
import sys
from collections import Counter, defaultdict

import psycopg

from .config import CACHE_DIR
from .extract import Extraction
from .load import load

DETAIL = CACHE_DIR / "dewiki_players_dryrun.json"
COMPARED = ["predicate", "subject_id", "object_id", "start_year", "end_year", "start_date", "end_date",
            "date_quality", "apps", "goals", "qualifiers", "source", "stale"]


def _facts(cur) -> dict[str, dict]:
    cur.execute("select f.source_ref, f.id, s.wikidata_id, o.wikidata_id, " + ", ".join(f"f.{c}" for c in COMPARED) +
                " from facts f join entities s on s.id = f.subject_id left join entities o on o.id = f.object_id")
    out = {}
    for ref, fid, subj, obj, *vals in cur.fetchall():
        row = dict(zip(COMPARED, vals))
        row["subject"], row["object"] = subj, obj
        out[ref] = row
    return out


def _clubs(cur) -> dict[str, int]:
    cur.execute("select o.canonical_name, count(distinct f.subject_id) from facts f join entities o on o.id = f.object_id "
                "where f.predicate = 'played_for' and not f.stale group by 1")
    return dict(cur.fetchall())


def current_from_year(cur, tunables: dict) -> int | None:
    """The cutoff prompts/generate.py renders into stints(): the latest competition season in the
    data minus the tunable window."""
    cur.execute("select max(cs.season_start_year) from club_seasons cs join entities c on c.id = cs.competition_id "
                "where c.wikidata_id = %s", (tunables["ingest"]["competition_qid"],))
    latest = cur.fetchone()[0]
    return None if latest is None else latest - (tunables["prompts"]["open_ended_current_seasons"] - 1)


def _weak(cur, cutoff: int | None) -> dict[str, int]:
    if cutoff is None:
        return {}
    cur.execute("select weak_reasons from stints(%s) where predicate = 'played_for'", (cutoff,))
    c: Counter = Counter()
    for (reasons,) in cur.fetchall():
        c["stints"] += 1
        c["with_any_weak_reason"] += bool(reasons)
        for r in reasons:
            c[r] += 1
    return dict(c)


def _scope_people(cur) -> dict[str, str]:
    cur.execute("select e.wikidata_id, e.canonical_name from entities e where e.id in "
                "(select subject_id from facts where predicate in ('played_for', 'coached') and not stale)")
    return dict(cur.fetchall())


def _named(cur, cases: list[str]) -> dict:
    """{club:person -> [spells]} for CLUB:PERSON Wikidata id pairs; missing ids give an empty list."""
    out = {}
    for case in cases:
        club, _, person = case.partition(":")
        cur.execute("select s.wikidata_id, s.canonical_name, f.start_year, f.end_year, f.apps, f.goals, "
                    "f.date_quality, f.source, f.stale, f.source_ref, f.qualifiers "
                    "from facts f join entities s on s.id = f.subject_id join entities o on o.id = f.object_id "
                    "where s.wikidata_id = %s and o.wikidata_id = %s and f.predicate = 'played_for' "
                    "order by f.start_year nulls first, f.end_year nulls last", (person, club))
        out[case] = [dict(zip(["person", "name", "start", "end", "apps", "goals", "quality", "source", "stale",
                               "ref", "qualifiers"], r)) for r in cur.fetchall()]
    return out


def snapshot(cur, tunables: dict, cases: list[str]) -> dict:
    cutoff = current_from_year(cur, tunables)
    return {"facts": _facts(cur), "clubs": _clubs(cur), "weak": _weak(cur, cutoff), "cutoff": cutoff,
            "people": _scope_people(cur), "named": _named(cur, cases)}


def diff_facts(before: dict, after: dict) -> dict:
    added = {r: a for r, a in after.items() if r not in before}
    went_stale = {r: (before[r], a) for r, a in after.items() if r in before and not before[r]["stale"] and a["stale"]}
    revived = {r: (before[r], a) for r, a in after.items() if r in before and before[r]["stale"] and not a["stale"]}
    changed = {}
    for r, a in after.items():
        b = before.get(r)
        if b and not b["stale"] and not a["stale"]:
            cols = [c for c in COMPARED if b[c] != a[c]]
            if cols:
                changed[r] = (cols, b, a)
    return {"added": added, "went_stale": went_stale, "revived": revived, "changed": changed}


def _by(items, key) -> dict[str, int]:
    return dict(sorted(Counter(key(i) for i in items).items()))


def _spell(s: dict) -> str:
    q = s["qualifiers"]
    tags = [k for k in ("dewiki_filled", "dewiki_apps_filled", "wikidata_links", "dewiki") if k in q]
    return (f"{s['start']}–{s['end'] or ''} apps={s['apps']} goals={s['goals']} {s['quality']} {s['source']}"
            f"{' STALE' if s['stale'] else ''} {tags or ''}")


def _views(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("select e.wikidata_id, sum(v.views) from entity_pageviews v join entities e on e.id = v.entity_id "
                    "where v.wiki = 'de' group by 1")
        return {q: int(n) for q, n in cur.fetchall()}


def _stint_line(f: dict, names: dict[str, str]) -> str:
    q = f["qualifiers"]
    tags = [k for k in ("dewiki_filled", "dewiki_apps_filled", "wikidata_links") if k in q]
    return (f"{names.get(f['object'], f['object'])}: {f['start_year']}–{f['end_year'] or ''} apps={f['apps']} "
            f"{f['date_quality']} {f['source']} {tags or ''}")


def _extra_reports(conn, ex, before, after, leaving) -> dict:
    views = _views(conn)
    names = {q: e.canonical_name for q, e in ex.entities.items()}
    names.update(before["people"])
    st = ex.stats
    rows_by = defaultdict(list)       # person -> [(club, why, rows)]
    for person, club, _ref, _span, _q, rows in st.get("players_rule4_stint_dropped_reserve_youth_only", []):
        rows_by[person].append((club, "rule 4: only reserve/youth rows overlap the Wikidata stint", rows))
    for person, club, _links, rows in st.get("players_unproven_links_dropped_reserve_youth_rows_only", []):
        rows_by[person].append((club, "unproven link: only reserve/youth rows at the club", rows))
    for person, club, _links, rows in st.get("players_unproven_links_dropped_senior_rows_before_window", []):
        rows_by[person].append((club, "unproven link: senior rows only before the window", rows))
    for person, club, _ref in st.get("players_unproven_link_ended_before_window", []):
        rows_by[person].append((club, "unproven link: dewiki end year before the window", []))
    top = sorted(leaving, key=lambda q: -views.get(q, 0))[:25]
    leaving_top = [{"person": q, "name": names.get(q, q), "views": views.get(q, 0),
                    "reasons": sorted({w for _, w, _ in rows_by.get(q, [])}) or ["unknown"],
                    "clubs": [{"club": names.get(c, c), "why": w, "rows": r} for c, w, r in rows_by.get(q, [])]}
                   for q in top]
    staying = []
    for person, club, _ref, span, _q, rows in st.get("players_rule4_stint_dropped_reserve_youth_only", []):
        if person in after["people"]:
            staying.append({"person": person, "name": names.get(person, person), "views": views.get(person, 0),
                            "club": names.get(club, club), "stint": span, "rows": rows})
    staying = sorted(staying, key=lambda e: -e["views"])[:15]
    adds = list(st.get("players_rule5_rows_only_source", []))
    sample = random.Random(42).sample(adds, min(10, len(adds)))
    rule5 = []
    for person, club, row, _links in sample:
        others = [_stint_line(f, names) for f in after["facts"].values()
                  if f["subject"] == person and f["predicate"] == "played_for" and not f["stale"]]
        rule5.append({"person": person, "name": names.get(person, person), "club": names.get(club, club),
                      "row": row, "other_stints": sorted(others)})
    check = {}
    for case in after["named"]:
        person = case.partition(":")[2]
        check[case] = sorted(_stint_line(f, names) + " rows=" + json.dumps(
            f["qualifiers"].get("row") or [d["row"] for d in f["qualifiers"].get("dewiki", [])], ensure_ascii=False)
            for f in after["facts"].values()
            if f["subject"] == person and f["predicate"] == "played_for" and not f["stale"]) or ["no played_for stint"]
    return {"leaving_top": leaving_top, "rule4_staying_top": staying, "rule5_sample": rule5, "check_people": check}


def dry_run(conn: psycopg.Connection, ex: Extraction, pageviews: list | None, seconds: float,
            tunables: dict, cases: list[str]) -> None:
    with conn.cursor() as cur:
        before = snapshot(cur, tunables, cases)
    after = None
    with conn.transaction():
        counts = load(conn, ex, pageviews)
        with conn.cursor() as cur:
            after = snapshot(cur, tunables, cases)
        raise psycopg.Rollback()
    d = diff_facts(before["facts"], after["facts"])
    with conn.cursor() as cur:
        cur.execute("select count(*) from facts")
        n_now = cur.fetchone()[0]
    if n_now != len(before["facts"]):
        print(f"ERROR: the dry run changed the facts table ({len(before['facts'])} -> {n_now} rows); "
              "the rollback did not hold. Check the database before anything else.", file=sys.stderr)
        raise SystemExit(2)

    pred_src = lambda f: f"{f['predicate']}/{f['source']}"
    summary = {
        "rules": {k: v for k, v in sorted(ex.counts.items())
                  if k.startswith(("players_", "coach_end", "dewiki_player"))},
        "facts_added": _by(d["added"].values(), pred_src),
        "facts_gone_stale": _by((a for _, a in d["went_stale"].values()), pred_src),
        "facts_revived": _by((a for _, a in d["revived"].values()), pred_src),
        "facts_changed": _by((a for _, _, a in d["changed"].values()), pred_src),
        "facts_changed_by_columns": _by(d["changed"].values(), lambda t: ",".join(t[0])),
        "table_counts_after": counts,
    }
    entering = {q: n for q, n in after["people"].items() if q not in before["people"]}
    leaving = {q: n for q, n in before["people"].items() if q not in after["people"]}
    summary["people_in_scope"] = {"before": len(before["people"]), "after": len(after["people"]),
                                  "entering": len(entering), "leaving": len(leaving)}
    st = ex.stats
    reasons = {}
    for key, idx in (("players_rule4_stint_dropped_reserve_youth_only", 0),
                     ("players_unproven_links_dropped_reserve_youth_rows_only", 0),
                     ("players_unproven_links_dropped_senior_rows_before_window", 0),
                     ("players_unproven_links_dropped_senior_rows_covered_by_wikidata_stint", 0),
                     ("players_unproven_link_ended_before_window", 0)):
        for item in st.get(key, []):
            reasons.setdefault(item[idx], set()).add(key.removeprefix("players_"))
    summary["people_leaving_by_reason"] = _by(
        leaving, lambda q: "+".join(sorted(reasons.get(q, {"no dewiki reason (a Wikidata-only change?)"}))))
    gaps = Counter()
    for _, _, wd, rows in st.get("players_rule6_years_disagree", []):
        best = None
        for r in rows:
            ys = [int(y) for y in re.findall(r"\d{4}", r.split("|")[0])]
            if not ys:
                continue
            s0, e0 = ys[0], (ys[-1] if len(ys) > 1 or "–" not in r.split("|")[0] else None)
            gap = max(abs(s0 - (wd[0] or s0)), abs(e0 - wd[1]) if e0 and wd[1] else 99)
            best = gap if best is None else min(best, gap)
        gaps["1 year" if best == 1 else "2 years" if best == 2 else "3+ years" if best and best < 99
             else "open vs closed"] += 1
    summary["rule6_largest_gap_of_best_matching_row"] = dict(gaps)
    clubs = sorted(set(before["clubs"]) | set(after["clubs"]))
    summary["played_for_people_per_club"] = {c: [before["clubs"].get(c, 0), after["clubs"].get(c, 0)] for c in clubs}
    summary["weak_reasons_played_for"] = {"before": before["weak"], "after": after["weak"]}

    unread = ex.stats.get("dewiki_player_unreadable_rows", [])
    summary["unreadable_player_rows"] = {"count": len(unread), "by_reason": _by(unread, lambda r: r[3])}
    summary["category"] = {"members_total": ex.counts["dewiki_player_category_members_total"],
                           "unmapped_distinct": ex.counts["dewiki_player_category_unmapped_distinct"]}
    summary["http_requests"] = ex.stats.get("http_requests")
    summary["seconds"] = round(seconds)

    detail = {
        "summary": summary,
        "counts": dict(ex.counts),
        "stats": {k: v for k, v in ex.stats.items() if k.startswith(("players_", "coach_end", "dewiki_player"))},
        "facts_added": d["added"],
        "facts_gone_stale": {r: {"before": b} for r, (b, _) in d["went_stale"].items()},
        "facts_changed": {r: {"columns": cols, "before": b, "after": a} for r, (cols, b, a) in d["changed"].items()},
        "people_entering_scope": entering,
        "people_leaving_scope": leaving,
        "unreadable_player_rows": unread,
        "player_category_per_club": ex.stats.get("dewiki_player_category"),
        "named_cases": {"before": before["named"], "after": after["named"]},
    }
    extra = _extra_reports(conn, ex, before, after, leaving)
    detail["extra_reports"] = extra
    DETAIL.parent.mkdir(parents=True, exist_ok=True)
    DETAIL.write_text(json.dumps(detail, ensure_ascii=False, indent=1, default=str))

    out = sys.stdout
    print("=== DRY RUN (transaction rolled back, nothing written) ===", file=out)
    print(json.dumps({k: v for k, v in summary.items() if k != "played_for_people_per_club"},
                     ensure_ascii=False, indent=1, default=str), file=out)
    print("-- people entering scope (first 15) --", file=out)
    for q, n in list(entering.items())[:15]:
        print(f"  {q} {n}", file=out)
    print("-- people leaving scope (first 15) --", file=out)
    for q, n in list(leaving.items())[:15]:
        print(f"  {q} {n}", file=out)
    print("-- played_for people per club (before -> after) --", file=out)
    for c, (b, a) in summary["played_for_people_per_club"].items():
        print(f"  {c}: {b} -> {a}", file=out)
    print(f"-- cases CLUB:PERSON, before and after (current-season cutoff {after['cutoff']}) --", file=out)
    for key in before["named"]:
        print(f"  {key}", file=out)
        for label, snap in (("before", before), ("after", after)):
            spells = snap["named"][key]
            if not spells:
                print(f"    {label}: no played_for spell (or unknown id)", file=out)
            for e in spells:
                print(f"    {label}: {_spell({'start': e['start'], 'end': e['end'], 'apps': e['apps'], 'goals': e['goals'], 'quality': e['quality'], 'source': e['source'], 'stale': e['stale'], 'qualifiers': e['qualifiers']})}", file=out)
    print("-- 25 most-viewed people still leaving scope --", file=out)
    for e in extra["leaving_top"]:
        print(f"  {e['views']:>9} {e['person']} {e['name']}: {e['reasons']}", file=out)
        for c in e["clubs"]:
            print(f"      {c['club']}: {c['why']} rows={c['rows']}", file=out)
    print("-- 15 most-viewed people whose stints rule 4 drops while they stay in scope --", file=out)
    for e in extra["rule4_staying_top"]:
        print(f"  {e['views']:>9} {e['person']} {e['name']}: {e['club']} wikidata={e['stint']} rows={e['rows']}", file=out)
    print("-- 10 random rule-5 additions --", file=out)
    for e in extra["rule5_sample"]:
        print(f"  {e['person']} {e['name']} @ {e['club']}: {e['row']}", file=out)
        for sp in e["other_stints"]:
            print(f"      {sp}", file=out)
    print(f"details: {DETAIL}", file=out)
