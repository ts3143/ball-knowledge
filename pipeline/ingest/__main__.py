"""Bundesliga ingest: Wikidata (+ German Wikipedia coaching tables) -> entities, entity_aliases,
facts, entity_pageviews.

    uv run python -m ingest bundesliga                  # full run (re-runnable)
    uv run python -m ingest bundesliga --skip-pageviews
    uv run python -m ingest bundesliga --report-only    # summary of what's in the database
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import UTC, date, datetime

import psycopg

from .config import CACHE_DIR, RAW_DIR, load_settings
from .dewiki import DewikiApi, coach_stations
from .extract import (
    P_COACH_OF,
    P_FAMILY_NAME,
    P_HEAD_COACH,
    Q_SQUAD,
    Extraction,
    Scope,
    add_aliases,
    add_entity,
    item_values,
    parse_seasons,
    people_facts,
    season_facts,
    sitelink,
    snak_item,
    statements,
)
from .http import WikimediaClient
from .load import load
from .pageviews import Pageviews
from .report import print_report
from .sparql import Sparql, qid
from .wbapi import EntityApi

RUN_STATS = CACHE_DIR / "last_run_stats.json"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fill_missing_labels(api: EntityApi, entities: dict[str, dict], ids: list[str], languages: list[str]) -> None:
    """Entities with no label in the preferred languages get their labels in every language,
    so they can be named by one of those instead of a bare QID."""
    missing = [q for q in ids if not any(lang in entities[q].get("labels", {}) for lang in languages)]
    if missing:
        full = api.fetch(missing, "labels_any_language", with_claims=False, all_languages=True)
        for q in missing:
            if q in full:
                entities[q]["labels"] = full[q].get("labels", {})


def coach_tables(settings, client: WikimediaClient, clubs: dict[str, dict], club_ids: set[str],
                 squad_to_club: dict[str, str], persons: dict[str, dict], api: EntityApi,
                 ex: Extraction) -> list:
    """Infobox coaching rows at our clubs. Candidates are the articles in each club's dewiki coach
    category plus every person Wikidata links to a club as coach. People found only here are
    fetched from Wikidata and added to `persons`."""
    cfg = settings.ingest["dewiki"]
    dw = DewikiApi(client, cfg["api_endpoint"], settings.ingest["wikidata"]["api_endpoint"],
                   cfg["titles_per_request"], cfg["pause_seconds"])
    club_articles = {c: sitelink(clubs[c], "dewiki") for c in sorted(club_ids)}
    if missing := sorted(c for c, t in club_articles.items() if not t):
        raise SystemExit(f"Clubs without a German Wikipedia article: {missing}")
    club_titles = dw.club_titles(club_articles)

    targets = {*club_ids, *squad_to_club}
    coaches = {snak_item(st["mainsnak"]) for c in targets if c in clubs for st in statements(clubs[c], P_HEAD_COACH)}
    coaches |= {pid for pid, p in persons.items()
                if any(snak_item(st["mainsnak"]) in targets for st in statements(p, P_COACH_OF))}
    title_to_person = {t: pid for pid, p in persons.items() if p["id"] == pid and (t := sitelink(p, "dewiki"))}
    titles = {t for t, pid in title_to_person.items() if pid in coaches}
    for club, article in club_articles.items():
        members = dw.category_members(cfg["coach_category"].format(club=article))
        ex.counts["dewiki_category_members"] += len(members)
        if not members:
            ex.stats["dewiki_clubs_without_coach_category"].append(club)
        titles.update(members)
    pages = dw.pages(sorted(titles))
    ex.counts["dewiki_pages"] = len(pages)
    pages = {p.title: p for p in pages.values()}

    # Articles not yet linked to a fetched person: look up their Wikidata items and fetch them.
    unknown = sorted(t for t in pages if t not in title_to_person)
    new_ids = dw.wikidata_ids(unknown)
    fetched = api.fetch(sorted(set(new_ids.values()) - persons.keys()), "dewiki_people")
    persons.update(fetched)
    title_to_person.update(new_ids)
    ex.counts["dewiki_pages_without_wikidata_item"] = len(unknown) - len(new_ids)

    problems: list = []
    stations = coach_stations(pages, title_to_person, club_titles, ex.counts, problems)
    ex.stats["dewiki_unreadable_rows"] = problems
    return stations


def ingest(settings, skip_pageviews: bool) -> Extraction:
    ing = settings.ingest
    wd = ing["wikidata"]
    run_dir = RAW_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    client = WikimediaClient(settings.wikimedia_contact, wd["timeout_seconds"], wd["max_retries"], raw_dir=run_dir)
    sparql = Sparql(client, wd["sparql_endpoint"], wd["sparql_pause_seconds"])
    api = EntityApi(client, wd["api_endpoint"], wd["wbgetentities_batch"], wd["api_pause_seconds"],
                    ing["label_languages"])
    scope = Scope(ing["start_year"], ing["undated_min_birth_year"], tuple(ing["label_languages"]))
    competition_id = ing["competition_qid"]
    ex = Extraction()
    log(f"Raw responses: {run_dir}")

    log("1/7 Seasons and clubs (SPARQL)")
    rows = sparql.select("seasons", "seasons", competition=competition_id, start_year=scope.start_year,
                         squad_class=Q_SQUAD)
    seasons, squad_to_club = parse_seasons(rows, qid)
    winners = sparql.select("winners", "winners", competition=competition_id, start_year=scope.start_year)
    club_ids = {c for s in seasons.values() for c in s.teams.values()}
    season_facts(seasons, winners, competition_id, squad_to_club, qid, ex)
    ex.stats["squad_items"] = sorted(squad_to_club.items())
    squads_by_club: dict[str, list[str]] = defaultdict(list)
    for squad, club in squad_to_club.items():
        squads_by_club[club].append(squad)
    log(f"  {len(seasons)} seasons, {len(club_ids)} clubs, {len(squad_to_club)} squad items")

    log("2/7 People per club (SPARQL)")
    person_ids: set[str] = set()
    for i, club in enumerate(sorted(club_ids), 1):
        targets = " ".join(f"wd:{q}" for q in [club, *squads_by_club[club]])
        found = sparql.select("people", f"people_{club}", targets=targets, start_year=scope.start_year,
                              min_birth_year=scope.min_birth_year)
        found += sparql.select("coaches", f"coaches_{club}", targets=targets)
        person_ids.update(qid(r["person"]) for r in found)
        log(f"  {i}/{len(club_ids)} {club}: {len(found)} candidates ({len(person_ids)} total)")

    log("3/7 Entity details (wbgetentities)")
    clubs = api.fetch([*club_ids, *squad_to_club], "clubs")
    missing_clubs = club_ids - clubs.keys()
    if missing_clubs:
        raise SystemExit(f"Club items missing from Wikidata: {sorted(missing_clubs)}")
    competition = api.fetch([competition_id], "competition")[competition_id]
    persons = api.fetch(sorted(person_ids), "people")

    log("4/7 Coaching tables (German Wikipedia)")
    stations = coach_tables(settings, client, clubs, club_ids, squad_to_club, persons, api, ex)
    log(f"  {len(stations)} readable rows at our clubs")

    log("5/7 Scope rules")
    in_scope = people_facts(persons, clubs, squad_to_club, club_ids, scope, ex, stations)
    log(f"  {len(in_scope)} people in scope")

    log("6/7 Countries, positions, family names (wbgetentities)")
    attr_targets = {f.object for f in ex.facts.values() if f.predicate in ("nationality", "position")}
    family_ids = {fam for pid in in_scope for fam in item_values(persons[pid], P_FAMILY_NAME)}
    refs = api.fetch(sorted(attr_targets | family_ids), "refs", with_claims=False)
    for ref, f in list(ex.facts.items()):
        if f.predicate in ("nationality", "position"):
            if f.object not in refs:
                ex.counts[f"{f.predicate}_target_missing"] += 1
                del ex.facts[ref]
            else:
                f.object = refs[f.object]["id"]

    ex.counts["people_without_family_name"] = sum(1 for pid in in_scope if not item_values(persons[pid], P_FAMILY_NAME))
    langs = ing["label_languages"]
    fill_missing_labels(api, clubs, sorted(club_ids), langs)
    fill_missing_labels(api, persons, sorted(in_scope), langs)
    fill_missing_labels(api, refs, sorted({f.object for f in ex.facts.values()
                                           if f.predicate in ("nationality", "position")}), langs)

    for club in sorted(club_ids):
        add_entity(ex, clubs[club], "club", scope)
        add_aliases(ex, club, clubs[club], scope)
        for squad in squads_by_club[club]:
            add_aliases(ex, club, clubs[squad], scope)
    add_entity(ex, competition, "competition", scope)
    add_aliases(ex, competition_id, competition, scope)
    for pid in sorted(in_scope):
        add_entity(ex, persons[pid], "person", scope)
        add_aliases(ex, pid, persons[pid], scope, family_names=refs)
    for pred in ("nationality", "position"):
        targets = {f.object for f in ex.facts.values() if f.predicate == pred}
        for target in sorted(targets):
            add_entity(ex, refs[target], "country" if pred == "nationality" else "position", scope)
            add_aliases(ex, target, refs[target], scope)

    pageviews = None
    if not skip_pageviews:
        log("7/7 Pageviews (Wikimedia REST, cached)")
        pv_cfg = ing["pageviews"]
        pv_client = WikimediaClient(settings.wikimedia_contact, wd["timeout_seconds"], wd["max_retries"])
        articles = [(e.wikidata_id, wiki, title)
                    for e in ex.entities.values() if e.type in ("person", "club")
                    for wiki, title in (("de", e.dewiki_title), ("en", e.enwiki_title))
                    if title and wiki in pv_cfg["wikis"]]
        pageviews = Pageviews(pv_client, CACHE_DIR, pv_cfg, date.today()).fetch_all(articles)
    else:
        log("7/7 Pageviews skipped")
    ex.stats["pageview_rows"] = pageviews
    return ex


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ingest")
    parser.add_argument("competition", choices=["bundesliga"])
    parser.add_argument("--skip-pageviews", action="store_true", help="leave entity_pageviews as it is")
    parser.add_argument("--report-only", action="store_true", help="print the summary without ingesting")
    args = parser.parse_args()
    settings = load_settings()
    t0 = time.monotonic()

    with psycopg.connect(settings.database_url) as conn:
        if args.report_only:
            run = json.loads(RUN_STATS.read_text()) if RUN_STATS.exists() else None
        else:
            ex = ingest(settings, args.skip_pageviews)
            pageviews = ex.stats.pop("pageview_rows")
            log("Loading into Postgres")
            counts = load(conn, ex, pageviews)
            run = {"finished_at": datetime.now(UTC).isoformat(), "stats": ex.stats, "counts": dict(ex.counts),
                   "rows_written": {"entities": len(ex.entities), "entity_aliases": len(ex.aliases),
                                    "facts": len(ex.facts),
                                    "entity_pageviews": len(pageviews) if pageviews is not None else None},
                   "table_counts": counts}
            RUN_STATS.parent.mkdir(parents=True, exist_ok=True)
            RUN_STATS.write_text(json.dumps(run, ensure_ascii=False, indent=1, default=str))
            log(f"Done in {time.monotonic() - t0:.0f}s")
        print_report(conn, run, settings.tunables)


if __name__ == "__main__":
    main()
