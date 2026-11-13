"""Writes dives: `schedule` fills the real dates, `open-now` opens a test dive, `unschedule` takes one back.

Scheduling is publishing: once a prompt is in a dive its text, answer set and tiers are frozen (a database
trigger enforces it) and its rating period is that dive's date. Every function here writes in one transaction.
"""

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from prompts.difficulty import seed_difficulty

from .scheduler import Candidate, DiveRules, Picked, choose_dive, dive_date_at, next_test_date, window


class DiveError(Exception):
    """Something the operator can fix (no such competition, nothing to schedule, a dive that cannot be removed)."""


@dataclass(frozen=True)
class Competition:
    id: int
    slug: str
    name: str
    sport: str


@dataclass
class PlannedDive:
    dive_id: int | None  # None in a dry run
    day: date
    opens_at: datetime
    closes_at: datetime
    is_test: bool
    picked: Picked


@dataclass
class Plan:
    dives: list[PlannedDive] = field(default_factory=list)
    texts: dict[int, str] = field(default_factory=dict)  # prompt id -> text, of every prompt in the plan
    templates: dict[int, str] = field(default_factory=dict)
    unscheduled: list[date] = field(default_factory=list)  # wanted dates that got no dive: the pool ran dry
    notes: list[str] = field(default_factory=list)  # prompts left out of the pool and why
    pool_left: int = 0


def load_competition(cur, tunables: dict, slug: str) -> Competition:
    known = tunables.get("competitions", {})
    if slug not in known:
        raise DiveError(f"unknown competition {slug!r}; known: {', '.join(known) or 'none'}")
    c = known[slug]
    cur.execute("select id from entities where wikidata_id = %s and type = 'competition'", (c["qid"],))
    row = cur.fetchone()
    if row is None:
        raise DiveError(f"no competition entity {c['qid']} for {slug}; run the ingest first")
    return Competition(row["id"], slug, c["name"], c["sport"])


def load_pool(cur, comp: Competition, tunables: dict) -> tuple[list[Candidate], dict[int, str], list[str]]:
    """Approved prompts of the competition that no dive uses, with their seed difficulty; also their texts and
    notes on prompts skipped because they cannot be seeded."""
    cur.execute("select id, template, text, club_ids, rarity, gate_results from prompts p"
                " where status = 'approved' and competition_id = %s"
                " and not exists (select 1 from dive_prompts d where d.prompt_id = p.id) order by id", (comp.id,))
    pool, texts, notes = [], {}, []
    for r in cur.fetchall():
        seed = seed_difficulty(tunables, r["rarity"], r["gate_results"])
        if seed is None:
            notes.append(f"prompt {r['id']} has no seed difficulty (missing rarity features): left out")
            continue
        pool.append(Candidate(r["id"], r["template"], tuple(r["club_ids"]), seed))
        texts[r["id"]] = r["text"]
    return pool, texts, notes


def _insert_dive(cur, comp: Competition, day: date, opens_at: datetime, closes_at: datetime, is_test: bool,
                 picked: Picked) -> int:
    cur.execute("insert into dives (competition_id, sport, dive_date, opens_at, closes_at, is_test)"
                " values (%s, %s, %s, %s, %s, %s) returning id",
                (comp.id, comp.sport, day, opens_at, closes_at, is_test))
    dive_id = cur.fetchone()["id"]
    cur.executemany("insert into dive_prompts (dive_id, position, prompt_id) values (%s, %s, %s)",
                    [(dive_id, i, c.id) for i, c in enumerate(picked.candidates, 1)])
    return dive_id


def schedule(conn: psycopg.Connection, comp: Competition, tunables: dict, rules: DiveRules, seed: int | None = None,
             dry_run: bool = False, now: datetime | None = None) -> Plan:
    """Gives every date from today to today + queue_days - 1 that has no real dive one, from the approved,
    unused prompts. Test dives and their dates are ignored. With too few prompts it schedules as many dates as
    it can, in order, and lists the rest in plan.unscheduled."""
    now = now or datetime.now(UTC)
    rng = random.Random(rules.seed if seed is None else seed)
    plan = Plan()
    with conn.transaction(force_rollback=dry_run), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select dive_date from dives where competition_id = %s and not is_test", (comp.id,))
        have = {r["dive_date"] for r in cur.fetchall()}
        first = dive_date_at(now, rules)
        missing = [d for d in (first + timedelta(days=i) for i in range(rules.queue_days)) if d not in have]
        pool, texts, plan.notes = load_pool(cur, comp, tunables)
        for i, day in enumerate(missing):
            picked = choose_dive(pool, rules, rng)
            if picked is None:
                plan.unscheduled = missing[i:]
                break
            opens_at, closes_at = window(day, rules)
            dive_id = _insert_dive(cur, comp, day, opens_at, closes_at, False, picked)  # rolled back in a dry run
            plan.dives.append(PlannedDive(None if dry_run else dive_id, day, opens_at, closes_at, False, picked))
            used = {c.id for c in picked.candidates}
            pool = [c for c in pool if c.id not in used]
            plan.texts.update({c.id: texts[c.id] for c in picked.candidates})
            plan.templates.update({c.id: c.template for c in picked.candidates})
        plan.pool_left = len(pool)
    return plan


def open_now(conn: psycopg.Connection, comp: Competition, tunables: dict, rules: DiveRules,
             minutes: int | None = None, seed: int | None = None, now: datetime | None = None) -> Plan:
    """TEST MODE, local database only. A test dive that opens now and closes after `minutes`, from approved
    prompts no dive uses, chosen by the same rules as `schedule`. Its date is the next free virtual date of the
    reserved test range (before 2000), so it is its own rating period and never collides with a real date."""
    minutes = rules.test_dive_minutes if minutes is None else minutes
    if minutes < 1:
        raise DiveError("--minutes must be at least 1")
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    rng = random.Random(rules.seed if seed is None else seed)
    plan = Plan()
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select dive_date from dives where competition_id = %s and is_test", (comp.id,))
        day = next_test_date({r["dive_date"] for r in cur.fetchall()}, rules)
        if day is None:
            raise DiveError(f"the test date range {rules.test_date_start}..{rules.test_date_end} is full")
        pool, texts, plan.notes = load_pool(cur, comp, tunables)
        picked = choose_dive(pool, rules, rng)
        if picked is None:
            raise DiveError(not_enough_message(comp, len(pool), rules, 1))
        closes_at = now + timedelta(minutes=minutes)
        dive_id = _insert_dive(cur, comp, day, now, closes_at, True, picked)
        plan.dives.append(PlannedDive(dive_id, day, now, closes_at, True, picked))
        plan.texts = {c.id: texts[c.id] for c in picked.candidates}
        plan.templates = {c.id: c.template for c in picked.candidates}
        plan.pool_left = len(pool) - len(picked.candidates)
    return plan


def not_enough_message(comp: Competition, pool_size: int, rules: DiveRules, dives_wanted: int) -> str:
    need = rules.prompts_per_dive * dives_wanted
    return (f"No dive can be made for {comp.name}: {pool_size} approved prompt{'s' if pool_size != 1 else ''} "
            f"not used in any dive, and a dive needs {rules.prompts_per_dive} ({need} for {dives_wanted} "
            f"date{'s' if dives_wanted != 1 else ''}).\n"
            f"Approve more with `python -m prompts review`.")


def unschedule(conn: psycopg.Connection, comp: Competition, day: date, now: datetime | None = None) -> int:
    """Removes the dive of `day` and frees its prompts. A real dive only before it opens; a test dive (whose
    date is a virtual one) any time. Either way only while nobody has an attempt in it. Returns the number of
    prompts freed."""
    now = now or datetime.now(UTC)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select id, opens_at, is_test, (select count(*) from attempts a where a.dive_id = dives.id)"
                    " as attempts, (select count(*) from dive_prompts d where d.dive_id = dives.id) as prompts"
                    " from dives where competition_id = %s and dive_date = %s for update", (comp.id, day))
        dive = cur.fetchone()
        if dive is None:
            raise DiveError(f"no {comp.slug} dive on {day}")
        if dive["attempts"]:
            raise DiveError(f"the {day} dive has {dive['attempts']} attempt(s) and cannot be removed")
        if not dive["is_test"] and dive["opens_at"] <= now:
            raise DiveError(f"the {day} dive opened at {dive['opens_at']:%Y-%m-%d %H:%M %Z} and cannot be removed")
        cur.execute("delete from dives where id = %s", (dive["id"],))
        return dive["prompts"]
