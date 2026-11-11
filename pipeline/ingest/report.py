"""End-of-run summary: table counts, sample players to spot-check, and the coverage report.

Coverage findings are reported, never patched.
"""

import random
import textwrap

import psycopg

ALIAS_KINDS = ("label", "alt_label", "short_name", "nickname", "family_name", "translit")
DATE_QUALITIES = ("dated", "open_ended", "undated")


def season_name(start_year: int, end_year: int | None) -> str:
    return f"{start_year}–{str(end_year)[-2:]}" if end_year else f"{start_year}–"


def years(start: int | None, end: int | None) -> str:
    return f"{start or '?'}–{end or ''}"


def print_report(conn: psycopg.Connection, run: dict | None, tunables: dict) -> None:
    cov = tunables["ingest"]["coverage"]
    with conn.cursor() as cur:
        _counts(cur)
        _samples(cur, cov["sample_players"], cov["sample_seed"])
        _coverage(cur, run, tunables)


def _q(cur, sql: str, params=None) -> list[tuple]:
    cur.execute(sql, params)
    return cur.fetchall()


def _counts(cur) -> None:
    print("\n" + "=" * 78 + "\nCOUNTS\n" + "=" * 78)
    by_type = dict(_q(cur, "select type::text, count(*) from entities group by 1"))
    total = sum(by_type.values())
    print(f"entities         {total:>8}   " + ", ".join(f"{t} {by_type.get(t, 0)}"
                                                    for t in ("person", "club", "competition", "country", "position")))
    by_kind = dict(_q(cur, "select kind, count(*) from entity_aliases where not stale group by 1"))
    stale_aliases = _q(cur, "select count(*) from entity_aliases where stale")[0][0]
    print(f"entity_aliases   {sum(by_kind.values()):>8}   " + ", ".join(f"{k} {by_kind.get(k, 0)}" for k in ALIAS_KINDS)
          + f"   (+{stale_aliases} stale)")
    facts = _q(cur, "select predicate, coalesce(date_quality, '-'), count(*) from facts where not stale group by 1, 2")
    stale_facts = _q(cur, "select count(*) from facts where stale")[0][0]
    print(f"facts            {sum(n for *_, n in facts):>8}   (+{stale_facts} stale)")
    by_pred: dict[str, dict[str, int]] = {}
    for pred, quality, n in facts:
        by_pred.setdefault(pred, {})[quality] = n
    for pred in ("played_for", "coached", "competed_in", "won", "nationality", "position", "born"):
        q = by_pred.get(pred, {})
        detail = ", ".join(f"{k} {q[k]}" for k in DATE_QUALITIES if k in q) if pred in ("played_for", "coached") else ""
        print(f"  {pred:<14} {sum(q.values()):>8}   {detail}")
    pv = dict(_q(cur, "select wiki, count(*) from entity_pageviews group by 1"))
    print(f"entity_pageviews {sum(pv.values()):>8}   " + ", ".join(f"{w} {n}" for w, n in sorted(pv.items())))
    people = _q(cur, "select count(*) from entities where type = 'person'")[0][0]
    for wiki in ("de", "en"):
        n = _q(cur, "select count(distinct v.entity_id) from entity_pageviews v join entities e on e.id = v.entity_id "
                    "where e.type = 'person' and v.wiki = %s", (wiki,))[0][0]
        print(f"  people with {wiki} views: {n}/{people} ({100 * n / max(people, 1):.0f}%)")


def _samples(cur, n: int, seed: int) -> None:
    print("\n" + "=" * 78 + f"\nSAMPLE PLAYERS (seed {seed}; half from the top quartile by dewiki views)\n" + "=" * 78)
    players = _q(cur, """
        select e.id, e.wikidata_id, coalesce(sum(v.views) filter (where v.wiki = 'de'), 0) as de_views
        from entities e
        left join entity_pageviews v on v.entity_id = e.id
        where e.type = 'person'
          and exists (select 1 from facts f where f.subject_id = e.id and f.predicate = 'played_for' and not f.stale)
        group by e.id, e.wikidata_id
    """)
    if not players:
        print("(no players)")
        return
    players.sort(key=lambda p: int(p[1][1:]))
    rng = random.Random(seed)
    by_views = sorted(players, key=lambda p: -p[2])
    cutoff = by_views[len(by_views) // 4][2]
    top = [p for p in players if p[2] > cutoff]
    rest = [p for p in players if p[2] <= cutoff]
    picks = rng.sample(top, min(n // 2, len(top)))
    picks += rng.sample(rest, min(n - len(picks), len(rest)))
    for entity_id, _, _ in picks:
        _print_person(cur, entity_id)


def _print_person(cur, entity_id: int) -> None:
    (qid, name, de_title, en_title), = _q(cur, "select wikidata_id, canonical_name, dewiki_title, enwiki_title "
                                              "from entities where id = %s", (entity_id,))
    born = _q(cur, "select value_date, qualifiers->>'precision' from facts "
                   "where subject_id = %s and predicate = 'born' and not stale", (entity_id,))
    attrs = _q(cur, """select f.predicate, string_agg(o.canonical_name, ', ' order by o.canonical_name)
                       from facts f join entities o on o.id = f.object_id
                       where f.subject_id = %s and f.predicate in ('nationality', 'position') and not f.stale
                       group by 1""", (entity_id,))
    views = dict(_q(cur, "select wiki, sum(views) from entity_pageviews where entity_id = %s group by 1", (entity_id,)))
    attr = dict(attrs)
    b = born[0] if born else None
    born_s = (str(b[0])[:4] if b[1] == "9" else str(b[0])) if b else "?"
    print(f"\n{qid}  {name}   born {born_s} · {attr.get('nationality', 'no nationality')} · "
          f"{attr.get('position', 'no position')}")
    print(f"  dewiki: {de_title or '—'} ({views.get('de', 0):,} views)   enwiki: {en_title or '—'} "
          f"({views.get('en', 0):,} views)")
    stints = _q(cur, """select f.predicate, c.canonical_name, f.start_year, f.end_year, f.date_quality, f.apps,
                               f.qualifiers ? 'via_item'
                        from facts f join entities c on c.id = f.object_id
                        where f.subject_id = %s and f.predicate in ('played_for', 'coached') and not f.stale
                        order by f.start_year nulls first, f.predicate""", (entity_id,))
    for pred, club, start, end, quality, apps, via in stints:
        extra = (f"  {apps} apps" if apps is not None else "") + ("  (via squad item)" if via else "")
        flag = "" if quality == "dated" else f"  [{quality}]"
        print(f"  {pred:<10} {club:<28} {years(start, end):<10}{flag}{extra}")
    aliases = _q(cur, "select alias_norm, kind from entity_aliases where entity_id = %s and not stale "
                      "order by array_position(%s::text[], kind), alias_norm", (entity_id, list(ALIAS_KINDS)))
    print("  aliases: " + "; ".join(f"{a} ({k})" for a, k in aliases))


def _coverage(cur, run: dict | None, tunables: dict) -> None:
    ing = tunables["ingest"]
    cov = ing["coverage"]
    start_year = ing["start_year"]
    print("\n" + "=" * 78 + "\nCOVERAGE (reported, not patched)\n" + "=" * 78)

    if run:
        seasons = run["stats"].get("seasons", [])
        short = [s for s in seasons if s["teams"] < cov["teams_per_season"]]
        print(f"\n• Seasons since {start_year}: {len(seasons)}. With fewer than {cov['teams_per_season']} teams listed: "
              f"{len(short)}")
        for s in short:
            print(f"    {season_name(s['start_year'], s['end_year'])} ({s['qid']}): {s['teams']} teams")
        no_winner = [s for s in seasons if not s["winners"]]
        print(f"• Seasons with no winner (P1346): "
              f"{', '.join(season_name(s['start_year'], s['end_year']) for s in no_winner) or 'none'}")
        bad_winners = run["stats"].get("winners_not_a_club", [])
        if bad_winners:
            print(f"• Winner values that aren't a club in the set (skipped): {bad_winners}")
        squads = run["stats"].get("squad_items", [])
        squad_seasons = sum(s["squad_items"] for s in seasons)
        print(f"• Squad items resolved to their club (P831): {len(squads)} items, {squad_seasons} season listings")
        names = dict(_q(cur, "select wikidata_id, canonical_name from entities where type = 'club'"))
        by_club: dict[str, list[str]] = {}
        for squad, club in squads:
            by_club.setdefault(club, []).append(squad)
        line = ", ".join(f"{names.get(c, c)} ← {'/'.join(s)}" for c, s in sorted(by_club.items(), key=lambda kv: names.get(kv[0], kv[0])))
        print(f"    {line}")
        clubs_only_via_squad = _q(cur, """
            select c.canonical_name from entities c where c.type = 'club'
              and not exists (select 1 from facts f where f.subject_id = c.id and f.predicate = 'competed_in'
                              and f.qualifiers->>'listed_as' = c.wikidata_id)
            order by 1""")
        print(f"    clubs listed only through squad items: {', '.join(r[0] for r in clubs_only_via_squad) or 'none'}")

    # Date quality per club.
    rows = _q(cur, """
        select c.canonical_name,
               count(*) filter (where f.date_quality = 'dated'),
               count(*) filter (where f.date_quality = 'open_ended'),
               count(*) filter (where f.date_quality = 'undated'),
               count(*) filter (where f.apps is not null),
               count(*)
        from facts f join entities c on c.id = f.object_id
        where f.predicate = 'played_for' and not f.stale
        group by 1 order by 4::float / count(*) desc, 1""")
    total = [sum(r[i] for r in rows) for i in range(1, 6)]
    print(f"\n• Player stints by date quality (all clubs): dated {total[0]}, open-ended {total[1]}, undated {total[2]} "
          f"({100 * total[2] / max(total[4], 1):.0f}% undated); with appearance counts: {total[3]} "
          f"({100 * total[3] / max(total[4], 1):.0f}%)")
    print(f"    {'club':<30}{'dated':>7}{'open':>6}{'undated':>9}{'apps':>6}{'total':>7}")
    for name, dated, open_, undated, apps, n in rows:
        print(f"    {name[:29]:<30}{dated:>7}{open_:>6}{undated:>9}{apps:>6}{n:>7}")

    # Thin squads: players whose stint years span the season (start <= Y, end >= Y+1 or open).
    thin = _q(cur, """
        with seasons as (select subject_id as club_id, start_year as y from facts
                         where predicate = 'competed_in' and not stale)
        select c.canonical_name, s.y, count(distinct p.subject_id)
        from seasons s join entities c on c.id = s.club_id
        left join facts p on p.object_id = s.club_id and p.predicate = 'played_for' and not p.stale
             and p.start_year <= s.y and (p.end_year is null or p.end_year >= s.y + 1)
        group by 1, 2 having count(distinct p.subject_id) < %s
        order by 3, 1, 2""", (cov["min_squad_size"],))
    n_seasons = _q(cur, "select count(*) from facts where predicate = 'competed_in' and not stale")[0][0]
    print(f"\n• Thin club-seasons: {len(thin)} of {n_seasons} Bundesliga club-seasons have fewer than "
          f"{cov['min_squad_size']} players whose stint years span the season")
    for name, y, n in thin[:20]:
        print(f"    {name:<30} {season_name(y, y + 1)}: {n}")
    if len(thin) > 20:
        print(f"    … and {len(thin) - 20} more")

    no_coach = _q(cur, """
        select c.canonical_name, f.start_year from facts f join entities c on c.id = f.subject_id
        where f.predicate = 'competed_in' and not f.stale
          and not exists (select 1 from facts k where k.object_id = f.subject_id and k.predicate = 'coached'
                          and not k.stale and k.date_quality <> 'undated'
                          and coalesce(k.start_year, 0) <= f.start_year + 1
                          and coalesce(k.end_year, 9999) >= f.start_year)
        order by 1, 2""")
    print(f"\n• Club-seasons with no head coach overlapping them: {len(no_coach)} of {n_seasons}")
    grouped: dict[str, list[int]] = {}
    for name, y in no_coach:
        grouped.setdefault(name, []).append(y)
    for name, ys in grouped.items():
        print(f"    {name:<30} {', '.join(season_name(y, y + 1) for y in ys)}")

    stale_open = _q(cur, """
        select distinct on (a.id) e.canonical_name, ca.canonical_name, a.start_year, cb.canonical_name, b.start_year
        from facts a
        join facts b on b.subject_id = a.subject_id and b.predicate = 'played_for' and not b.stale
                    and b.object_id <> a.object_id and b.start_year > a.start_year
        join entities e on e.id = a.subject_id
        join entities ca on ca.id = a.object_id
        join entities cb on cb.id = b.object_id
        where a.predicate = 'played_for' and a.date_quality = 'open_ended' and not a.stale
        order by a.id, b.start_year""")
    open_total = _q(cur, "select count(*) from facts where predicate = 'played_for' "
                         "and date_quality = 'open_ended' and not stale")[0][0]
    print(f"\n• Open-ended stints that look stale (a later stint at another Bundesliga club): {len(stale_open)} "
          f"of {open_total} open-ended. Moves abroad can't be seen: other clubs aren't ingested.")
    for row in stale_open[:5]:
        print(f"    {row[0]}: {row[1]} from {row[2]}, no end; later {row[3]} from {row[4]}")

    ages = _q(cur, """
        select e.canonical_name, c.canonical_name, s.start_year, extract(year from b.value_date)::int
        from facts s join facts b on b.subject_id = s.subject_id and b.predicate = 'born' and not b.stale
        join entities e on e.id = s.subject_id join entities c on c.id = s.object_id
        where s.predicate = 'played_for' and not s.stale and s.start_year is not null
          and (s.start_year - extract(year from b.value_date) < %s or s.start_year - extract(year from b.value_date) > %s)
        order by 1""", (cov["min_plausible_age"], cov["max_plausible_age"]))
    print(f"\n• Player stints starting at an implausible age (<{cov['min_plausible_age']} or "
          f">{cov['max_plausible_age']}): {len(ages)}. Mostly youth-academy years recorded against the senior club.")
    for name, club, y, by in ages[:8]:
        print(f"    {name}: {club} from {y}, born {by} (age {y - by})")

    apps = dict(_q(cur, """select case when apps is null then 'none' when apps = 0 then 'zero' else 'some' end, count(*)
                           from facts where predicate = 'played_for' and not stale group by 1"""))
    print(f"\n• Player stints by recorded appearances (P1350): {apps.get('some', 0)} with apps > 0, "
          f"{apps.get('zero', 0)} with 0 apps (squad or youth player who never played?), {apps.get('none', 0)} with no count")

    dupes = _q(cur, """select p.canonical_name, c.canonical_name, f.start_year, f.end_year, count(*)
                       from facts f join entities p on p.id = f.subject_id join entities c on c.id = f.object_id
                       where f.predicate = 'played_for' and not f.stale and f.start_year is not null
                       group by f.subject_id, f.object_id, f.start_year, f.end_year, 1, 2 having count(*) > 1
                       order by 1""")
    print(f"\n• Stints stated twice on Wikidata (same person, club and years; kept as separate facts): {len(dupes)}")
    for name, club, s_, e, n in dupes[:5]:
        print(f"    {name}: {club} {years(s_, e)} ×{n}")

    backwards = _q(cur, """select e.canonical_name, c.canonical_name, f.start_year, f.end_year from facts f
                           join entities e on e.id = f.subject_id join entities c on c.id = f.object_id
                           where f.predicate in ('played_for', 'coached') and not f.stale and f.end_year < f.start_year""")
    print(f"\n• Stints that end before they start: {len(backwards)}")
    for name, club, s, e in backwards[:5]:
        print(f"    {name}: {club} {s}–{e}")

    if run:
        c = run["counts"]
        print("\n• Excluded by the scope rules:")
        print(f"    women linked to men's club items: {c.get('women_on_mens_club_items', 0)} people")
        print(f"    people fetched but with no stint in scope: {c.get('people_dropped', 0)} "
              f"(birth date unknown: {c.get('people_dropped_birth_unknown', 0)}; "
              f"born before {ing['undated_min_birth_year']} with unproven dates: {c.get('people_dropped_born_before_cutoff', 0)}; "
              f"ended before {start_year}: {c.get('people_dropped_ended_before_window', 0)}). The SPARQL step already "
              f"leaves out people whose links all end before {start_year}, so those are mostly not counted here.")
        kept = c.get("played_for:in_by_birth_year", 0)
        print(f"    player stints kept only by the birth-year rule: {kept}; dropped as unproven: "
              f"{c.get('played_for:unproven_dates_born_before_cutoff', 0) + c.get('played_for:unproven_dates_birth_unknown', 0)}")
        print(f"    deprecated statements skipped: {c.get('deprecated_skipped', 0)}")
        print(f"    stints recorded against a squad item: {c.get('played_for:via_squad_item', 0)} player, "
              f"{c.get('coached_p286:via_squad_item', 0) + c.get('coached_p6087:via_squad_item', 0)} coach")
        names = dict(_q(cur, "select wikidata_id, canonical_name from entities where type in ('person', 'club')"))
        _coach_sources(cur, run, ing, names)
        unreadable = run["stats"].get("unreadable_dates", [])
        print(f"\n• Stint dates that can't be read (treated as unknown): {len(unreadable)}")
        for person, club, statement, raw in unreadable[:10]:
            print(f"    {names.get(person, person)} at {names.get(club, club)}: {', '.join(raw)}  ({statement})")
        no_label = run["stats"].get("no_label", [])
        if no_label:
            print("• Entities with no en/de/mul label, named from another language as-is: "
                  + ", ".join(f"{q} ({lang}: {name})" for q, lang, name in no_label))

    missing = _q(cur, """
        select count(*),
               count(*) filter (where not exists (select 1 from entity_aliases a where a.entity_id = e.id
                                                  and a.kind = 'family_name' and not a.stale)),
               count(*) filter (where not exists (select 1 from facts f where f.subject_id = e.id
                                                  and f.predicate = 'nationality' and not f.stale)),
               count(*) filter (where not exists (select 1 from facts f where f.subject_id = e.id
                                                  and f.predicate = 'born' and not f.stale)),
               count(*) filter (where e.dewiki_title is null),
               count(*) filter (where e.enwiki_title is null)
        from entities e where e.type = 'person'""")[0]
    no_position = _q(cur, """
        select count(*) from entities e where e.type = 'person'
          and exists (select 1 from facts f where f.subject_id = e.id and f.predicate = 'played_for' and not f.stale)
          and not exists (select 1 from facts f where f.subject_id = e.id and f.predicate = 'position' and not f.stale)
    """)[0][0]
    n = max(missing[0], 1)
    no_family = run["counts"].get("people_without_family_name", missing[1]) if run else missing[1]
    print(f"\n• People missing data (of {missing[0]}):")
    for label, k in (("family name (P734)", no_family), ("nationality", missing[2]),
                     ("birth date", missing[3]), ("position (players)", no_position),
                     ("dewiki article", missing[4]), ("enwiki article", missing[5])):
        print(f"    {label:<32} {k:>6} ({100 * k / n:.0f}%)")

    shared = _q(cur, """
        select a.alias_norm, count(distinct a.entity_id) from entity_aliases a
        join entities e on e.id = a.entity_id and e.type = 'person'
        where not a.stale group by 1 having count(distinct a.entity_id) > 1
        order by 2 desc, 1 limit 10""")
    total_shared = _q(cur, """select count(*) from (select alias_norm from entity_aliases a
                              join entities e on e.id = a.entity_id and e.type = 'person' where not a.stale
                              group by 1 having count(distinct a.entity_id) > 1) t""")[0][0]
    print(f"\n• Aliases shared by more than one person: {total_shared} (the matcher's \"ambiguous surname\" "
          f"cases). Most shared: " + ", ".join(f"{a} ({k})" for a, k in shared))


def _coach_sources(cur, run: dict, ing: dict, names: dict[str, str]) -> None:
    c, stats = run["counts"], run["stats"]

    def name(q: str) -> str:
        return names.get(q, q)

    by_source = dict(_q(cur, """
        select case when f.source = 'dewiki' and f.qualifiers ? 'wikidata_links' then 'dewiki_dates_link'
                    when f.source = 'dewiki' then 'dewiki_only'
                    when f.qualifiers ? 'dewiki' then 'wikidata_confirmed'
                    else 'wikidata_only' end, count(*)
        from facts f where f.predicate = 'coached' and not f.stale group by 1"""))
    print("\n• Coach spells by source:")
    for key, label in (("wikidata_only", "Wikidata only"),
                       ("wikidata_confirmed", "Wikidata, same spell in the dewiki infobox"),
                       ("dewiki_dates_link", "dewiki infobox years for an undated Wikidata link"),
                       ("dewiki_only", "dewiki infobox only (no Wikidata link)")):
        print(f"    {label:<52} {by_source.get(key, 0):>5}")
    print(f"  P6087 entries merged into an overlapping P286 spell: {c.get('coach_entries_collapsed', 0)}; "
          f"P6087-only spells kept: {c.get('coach_spells_only_in_p6087', 0)}")
    cutoff = ing["undated_min_birth_year"]

    def k(reason: str) -> int:
        return c.get(f"coached_p286:{reason}", 0) + c.get(f"coached_p6087:{reason}", 0)

    print(f"  Wikidata coach links without dates that prove {ing['start_year']}+:")
    print(f"    dated by the person's infobox rows at that club      {c.get('coach_links:dated_by_dewiki', 0):>5}")
    print(f"    dropped: infobox head-coach rows all before {ing['start_year']}     "
          f"{c.get('coach_links:dewiki_rows_before_window', 0):>5}")
    print(f"    dropped: infobox has only assistant/youth/reserve rows {c.get('coach_links:dewiki_rows_not_head_coach', 0):>5}")
    print(f"    no infobox rows; kept, flagged, born {cutoff}+           {k('in_by_birth_year'):>5}")
    print(f"    no infobox rows; dropped, born before {cutoff} or unknown {k('unproven_dates_born_before_cutoff') + k('unproven_dates_birth_unknown'):>5}")
    for key, label in (("coach_links_dewiki_rows_before_window", "before the window"),
                       ("coach_links_dewiki_rows_not_head_coach", "not head coach")):
        rows = stats.get(key, [])
        if rows:
            print(f"    e.g. {label}: " + "; ".join(f"{name(p)} at {name(cl)} [{' / '.join(r)}]" for p, cl, r in rows[:3]))

    non_head = stats.get("coach_statements_non_head_role", [])
    print(f"  Wikidata coach statements rejected, P39 qualifier says not head coach (P6087 "
          f"{c.get('coached_p6087:non_head_role', 0)}, P286 {c.get('coached_p286:non_head_role', 0)}): {len(non_head)}")
    for p, cl, _, roles in non_head[:8]:
        print(f"    {name(p)} at {name(cl)} [{' / '.join(roles)}]")

    dis = stats.get("coach_dewiki_disagreements", [])
    print(f"  Same spell, Wikidata and infobox years differ (Wikidata kept): {len(dis)}")
    for p, cl, wd, dw, _ in dis[:8]:
        print(f"    {name(p)} at {name(cl)}: Wikidata {years(*wd)}, infobox {years(*dw)}")
    other = stats.get("coach_spells_dewiki_says_other_role", [])
    print(f"  Wikidata coach spells dropped, the infobox shows only an assistant/youth/reserve role: {len(other)}")
    for p, cl, wd, rows in other[:8]:
        print(f"    {name(p)} at {name(cl)}: Wikidata {years(*wd)}, infobox [{' / '.join(rows)}]")

    problems = stats.get("dewiki_unreadable_rows", [])
    print(f"  Infobox reading: {c.get('dewiki_pages', 0)} articles "
          f"({c.get('dewiki_pages_with_coaching_table', 0)} with a coaching table, "
          f"{c.get('dewiki_pages_without_coaching_table', 0)} without); "
          f"{c.get('dewiki_pages_without_wikidata_item', 0)} have no Wikidata item; "
          f"rows at our clubs: " + ", ".join(f"{r} {c.get(f'dewiki_rows_at_our_clubs:{r}', 0)}"
                                           for r in ("head", "interim", "other")))
    print(f"  Rows at our clubs that couldn't be used: {len(problems)}")
    for reason in sorted({r for *_, r in problems}):
        rows = [x for x in problems if x[3] == reason]
        print(f"    {reason}: {len(rows)}, e.g. " + "; ".join(f"{t}: {y!r} {team}" for t, y, team, _ in rows[:3]))

    only = _q(cur, """
        select c.canonical_name, p.canonical_name, f.start_year, f.end_year, f.qualifiers->>'role'
        from facts f join entities p on p.id = f.subject_id join entities c on c.id = f.object_id
        where f.predicate = 'coached' and not f.stale and f.source = 'dewiki'
          and not f.qualifiers ? 'wikidata_links'
        order by 1, 3, 2, 4 nulls last, 5""")
    print(f"\n• Coach spells only in the German Wikipedia infobox, for checking ({len(only)}):")
    by_club: dict[str, list[str]] = {}
    for club, person, sy, ey, role in only:
        span = f"{sy}" if sy == ey else f"{sy}–{str(ey)[-2:] if ey else ''}"
        by_club.setdefault(club, []).append(f"{person} {span}{' (interim)' if role == 'interim' else ''}")
    for club, spells in by_club.items():
        print(textwrap.fill(", ".join(spells), width=110, initial_indent=f"    {club}: ", subsequent_indent="      "))
