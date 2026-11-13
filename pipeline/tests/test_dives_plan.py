"""Scheduling, test mode and unscheduling against placeholder prompts inside a rolled-back transaction.
Needs the local database."""

import tomllib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from psycopg import errors

from dives.plan import DiveError, load_competition, open_now, schedule, unschedule
from dives.scheduler import DiveRules, dive_date_at, window
from ingest.config import REPO_ROOT
from tests.db_dives import add_attempt, add_dive, add_prompt, add_user

pytestmark = pytest.mark.db

NOW = datetime(2030, 6, 10, 15, 30, tzinfo=UTC)  # 11:30 EDT: the dive date is 2030-06-10
TEMPLATES = ["club_pair", "club_nationality", "club_coaches", "bundesliga_nationality", "club_title_winners"]


@pytest.fixture
def setup(world):
    """(world, tunables with a competition 'testliga' pointing at the world's, rules)."""
    tunables = tomllib.loads((REPO_ROOT / "tunables.toml").read_text())
    tunables["competitions"] = {"testliga": {"qid": world.comp["wikidata_id"], "name": "Test League",
                                             "sport": "football"}}
    comp = load_competition(world.cur, tunables, "testliga")
    return world, tunables, DiveRules.from_tunables(tunables), comp


def fill_pool(w, n: int, status: str = "approved", first: int = 1) -> list[int]:
    """n prompts with rising seeds, templates in turn and a club each (clubs A-E)."""
    return [add_prompt(w, i, status=status, template=TEMPLATES[i % 5], clubs=("ABCDE"[i % 5],))
            for i in range(first, first + n)]


def dives(w, **where):
    return w.cur.execute("select * from dives where competition_id = %s and is_test = %s order by dive_date",
                         (w.comp["id"], where.get("is_test", False))).fetchall()


def dive_prompts(w, dive_id):
    return [r["prompt_id"] for r in w.cur.execute(
        "select prompt_id from dive_prompts where dive_id = %s order by position", (dive_id,)).fetchall()]


def test_schedule_fills_the_next_seven_dates_with_seven_unique_approved_prompts(conn, setup):
    w, tunables, rules, comp = setup
    pool = fill_pool(w, 60)
    fill_pool(w, 5, status="gate_passed", first=100)  # not approved: never scheduled
    plan = schedule(conn, comp, tunables, rules, now=NOW)

    assert [d.day for d in plan.dives] == [date(2030, 6, 10) + timedelta(days=i) for i in range(7)]
    assert plan.unscheduled == [] and plan.pool_left == 60 - 49
    rows = dives(w)
    assert [r["dive_date"] for r in rows] == [d.day for d in plan.dives]
    used = []
    for row in rows:
        ids = dive_prompts(w, row["id"])
        assert len(ids) == 7 and set(ids) <= set(pool)
        seeds = [c.seed for c in next(d for d in plan.dives if d.day == row["dive_date"]).picked.candidates]
        assert seeds == sorted(seeds)  # easiest first
        used += ids
        assert (row["opens_at"], row["closes_at"]) == window(row["dive_date"], rules)
        assert row["sport"] == "football" and row["settled_at"] is None and not row["is_test"]
    assert len(set(used)) == 49  # each prompt in one dive only


def test_a_scheduled_prompt_is_frozen(conn, setup):
    w, tunables, rules, comp = setup
    fill_pool(w, 49)
    schedule(conn, comp, tunables, rules, now=NOW)
    [first] = w.cur.execute("select prompt_id from dive_prompts limit 1").fetchall()
    with pytest.raises(errors.RestrictViolation), conn.transaction():
        conn.execute("update prompts set text = 'changed' where id = %s", (first["prompt_id"],))


def test_a_dry_run_writes_nothing_but_plans_the_same_dives(conn, setup):
    w, tunables, rules, comp = setup
    fill_pool(w, 60)
    dry = schedule(conn, comp, tunables, rules, dry_run=True, now=NOW)
    assert len(dry.dives) == 7 and all(d.dive_id is None for d in dry.dives)
    assert dives(w) == []
    real = schedule(conn, comp, tunables, rules, now=NOW)
    assert [[c.id for c in d.picked.candidates] for d in real.dives] == [
        [c.id for c in d.picked.candidates] for d in dry.dives]


def test_a_second_run_changes_nothing_and_a_gap_is_refilled_alone(conn, setup):
    w, tunables, rules, comp = setup
    fill_pool(w, 70)
    schedule(conn, comp, tunables, rules, now=NOW)
    again = schedule(conn, comp, tunables, rules, now=NOW)
    assert again.dives == [] and again.unscheduled == []
    before = {r["dive_date"]: dive_prompts(w, r["id"]) for r in dives(w)}
    assert unschedule(conn, comp, date(2030, 6, 13), now=NOW) == 7
    refill = schedule(conn, comp, tunables, rules, now=NOW)
    assert [d.day for d in refill.dives] == [date(2030, 6, 13)]
    after = {r["dive_date"]: dive_prompts(w, r["id"]) for r in dives(w)}
    assert {d: p for d, p in after.items() if d != date(2030, 6, 13)} == {
        d: p for d, p in before.items() if d != date(2030, 6, 13)}


def test_with_too_few_prompts_it_schedules_what_it_can_and_never_a_short_dive(conn, setup):
    w, tunables, rules, comp = setup
    fill_pool(w, 20)  # two dives, 6 left over
    plan = schedule(conn, comp, tunables, rules, now=NOW)
    assert len(plan.dives) == 2 and plan.pool_left == 6
    assert plan.unscheduled == [date(2030, 6, 12) + timedelta(days=i) for i in range(5)]
    assert all(len(dive_prompts(w, r["id"])) == 7 for r in dives(w))
    assert len(dives(w)) == 2


def test_no_approved_prompts_means_no_dives(conn, setup):
    w, tunables, rules, comp = setup
    fill_pool(w, 20, status="gate_passed")
    plan = schedule(conn, comp, tunables, rules, now=NOW)
    assert plan.dives == [] and len(plan.unscheduled) == 7 and plan.pool_left == 0
    assert dives(w) == []


def test_a_prompt_without_seed_features_is_left_out_with_a_note(conn, setup):
    w, tunables, rules, comp = setup
    fill_pool(w, 7)
    broken = add_prompt(w, 50)
    conn.execute("update prompts set rarity = '{\"model_version\": \"test\"}'::jsonb where id = %s", (broken,))
    plan = schedule(conn, comp, tunables, rules, now=NOW)
    assert len(plan.dives) == 1 and broken not in {c.id for c in plan.dives[0].picked.candidates}
    assert plan.notes == [f"prompt {broken} has no seed difficulty (missing rarity features): left out"]


def test_scheduling_is_per_competition(conn, setup):
    w, tunables, rules, comp = setup
    other = w.entity("competition", "Placeholder Other League")
    for i in range(1, 8):
        add_prompt(w, i, competition=other)
    plan = schedule(conn, comp, tunables, rules, now=NOW)
    assert plan.dives == []  # the other competition's prompts are not in this pool


def test_unschedule_only_before_opening_and_with_no_attempts(conn, setup):
    w, tunables, rules, comp = setup
    pool = fill_pool(w, 14)
    plan = schedule(conn, comp, tunables, rules, now=NOW)
    day1, day2 = plan.dives[0].day, plan.dives[1].day
    with pytest.raises(DiveError, match="opened at"):
        unschedule(conn, comp, day1, now=window(day1, rules)[0] + timedelta(seconds=1))
    with pytest.raises(DiveError, match="no testliga dive on 2031-01-01"):
        unschedule(conn, comp, date(2031, 1, 1), now=NOW)
    # a player with an attempt (even an unrated archive one) keeps the dive
    add_attempt(w, add_user(w), plan.dives[1].dive_id, rated=False)
    with pytest.raises(DiveError, match="1 attempt"):
        unschedule(conn, comp, day2, now=NOW - timedelta(days=30))
    assert unschedule(conn, comp, day1, now=window(day1, rules)[0] - timedelta(seconds=1)) == 7
    assert [r["dive_date"] for r in dives(w)] == [day2]
    # its prompts are free again, and no longer frozen
    freed = set(pool) - set(dive_prompts(w, dives(w)[0]["id"]))
    assert len(freed) == 7
    conn.execute("update prompts set text = 'edited' where id = %s", (next(iter(freed)),))


def test_open_now_makes_an_open_test_dive_on_the_next_virtual_date(conn, setup):
    w, tunables, rules, comp = setup
    pool = fill_pool(w, 30)
    first = open_now(conn, comp, tunables, rules, now=NOW)
    second = open_now(conn, comp, tunables, rules, minutes=5, seed=4, now=NOW + timedelta(seconds=1))
    assert [d.day for d in first.dives + second.dives] == [date(1900, 1, 1), date(1900, 1, 2)]
    t1, t2 = dives(w, is_test=True)
    assert (t1["opens_at"], t1["closes_at"], t1["is_test"]) == (NOW, NOW + timedelta(minutes=30), True)
    assert t2["closes_at"] - t2["opens_at"] == timedelta(minutes=5)
    ids1, ids2 = dive_prompts(w, t1["id"]), dive_prompts(w, t2["id"])
    assert len(ids1) == len(ids2) == 7 and not set(ids1) & set(ids2) and set(ids1 + ids2) <= set(pool)
    assert second.pool_left == 30 - 14
    assert all(d.is_test for d in first.dives)


def test_virtual_dates_never_collide_with_real_dates_and_real_scheduling_ignores_test_dives(conn, setup):
    w, tunables, rules, comp = setup
    fill_pool(w, 70)
    for i in range(3):
        open_now(conn, comp, tunables, rules, now=NOW + timedelta(seconds=i))
    plan = schedule(conn, comp, tunables, rules, now=NOW)  # 70 - 21 test prompts = 49: exactly 7 real dives
    assert len(plan.dives) == 7 and plan.unscheduled == []
    assert dive_date_at(NOW, rules) == plan.dives[0].day
    real, test = dives(w), dives(w, is_test=True)
    assert all(r["dive_date"] >= date(2000, 1, 1) for r in real)
    assert all(r["dive_date"] < date(2000, 1, 1) for r in test)
    assert {r["dive_date"] for r in real}.isdisjoint({r["dive_date"] for r in test})
    # a test dive and a real one never share a prompt
    real_prompts = {p for r in real for p in dive_prompts(w, r["id"])}
    test_prompts = {p for r in test for p in dive_prompts(w, r["id"])}
    assert len(real_prompts) == 49 and len(test_prompts) == 21 and not real_prompts & test_prompts


def test_open_now_fails_clearly_without_enough_prompts_or_dates(conn, setup):
    w, tunables, rules, comp = setup
    fill_pool(w, 6)
    with pytest.raises(DiveError, match="6 approved prompts not used in any dive"):
        open_now(conn, comp, tunables, rules, now=NOW)
    with pytest.raises(DiveError, match="at least 1"):
        open_now(conn, comp, tunables, rules, minutes=0, now=NOW)
    fill_pool(w, 20, first=10)
    tiny = replace(rules, test_date_end=date(1900, 1, 1))
    open_now(conn, comp, tunables, tiny, now=NOW)
    with pytest.raises(DiveError, match="test date range 1900-01-01..1900-01-01 is full"):
        open_now(conn, comp, tunables, tiny, now=NOW)


def test_a_test_dive_can_be_removed_while_nobody_has_played_it(conn, setup):
    w, tunables, rules, comp = setup
    fill_pool(w, 7)
    open_now(conn, comp, tunables, rules, now=NOW)
    assert unschedule(conn, comp, date(1900, 1, 1), now=NOW + timedelta(minutes=1)) == 7  # it is already open
    open_now(conn, comp, tunables, rules, now=NOW)
    [dive] = dives(w, is_test=True)
    add_attempt(w, add_user(w), dive["id"])
    with pytest.raises(DiveError, match="1 attempt"):
        unschedule(conn, comp, date(1900, 1, 1), now=NOW)


def test_the_database_keeps_test_and_real_dates_apart(conn, world):
    with pytest.raises(errors.CheckViolation, match="dives_test_date_range"), conn.transaction():
        add_dive(world, date(1900, 1, 5), [], is_test=False)


def test_an_unknown_competition_is_reported(conn, setup):
    w, tunables, _, _ = setup
    with pytest.raises(DiveError, match="unknown competition 'nope'; known: testliga"):
        load_competition(w.cur, tunables, "nope")
    tunables["competitions"]["testliga"]["qid"] = "Q1"
    with pytest.raises(DiveError, match="no competition entity Q1"):
        load_competition(w.cur, tunables, "testliga")
