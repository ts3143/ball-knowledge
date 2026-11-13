"""The dive rules on synthetic prompts: quota, caps, relaxation, ordering, never fewer than 7, and the dates.
No database."""

import random
import tomllib
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest

from dives.scheduler import (
    ATTEMPTS,
    Candidate,
    DiveRules,
    choose_dive,
    dive_date_at,
    next_test_date,
    window,
)
from ingest.config import REPO_ROOT

TUNABLES = tomllib.loads((REPO_ROOT / "tunables.toml").read_text())
RULES = DiveRules.from_tunables(TUNABLES)
TEMPLATES = ["t_pair", "t_nation", "t_coach", "t_league", "t_title"]


def pool_of(n: int, templates=TEMPLATES, clubs_per=1, club_count=30) -> list[Candidate]:
    """n prompts with rising seeds, templates in turn, and clubs spread so no caps bind."""
    return [Candidate(i, templates[i % len(templates)],
                      tuple((i * 7 + k) % club_count for k in range(clubs_per)), 1500.0 + i)
            for i in range(1, n + 1)]


def pick(pool, rules=RULES, seed=1):
    return choose_dive(pool, rules, random.Random(seed))


def test_a_dive_takes_the_band_quota_with_no_warnings():
    picked = pick(pool_of(60))
    assert len(picked.candidates) == 7 and picked.warnings == []
    counts = [sum(b == band for b in picked.bands.values()) for band in ("easy", "medium", "hard")]
    assert counts == [2, 3, 2]
    # bands are terciles of the pool: seeds 1501-1520 easy, 1521-1540 medium, 1541-1560 hard
    for c in picked.candidates:
        assert picked.bands[c.id] == ("easy" if c.seed <= 1520 else "medium" if c.seed <= 1540 else "hard")


def test_prompts_run_from_easiest_to_hardest():
    for seed in range(5):
        seeds = [c.seed for c in pick(pool_of(60), seed=seed).candidates]
        assert seeds == sorted(seeds)
        assert [pick(pool_of(60), seed=seed).bands[c.id] for c in pick(pool_of(60), seed=seed).candidates] == (
            ["easy"] * 2 + ["medium"] * 3 + ["hard"] * 2)


def test_at_most_two_per_template_and_two_per_club():
    templates = ["t_a", "t_b", "t_c", "t_d"]
    for seed in range(20):
        # every prompt names two of only 10 clubs, so the club cap binds (7 prompts need 14 of the 20 slots)
        pool = [Candidate(i, templates[i % 4], (i % 10, (i + 3) % 10), 1500.0 + i) for i in range(1, 61)]
        picked = pick(pool, seed=seed)
        by_template, by_club = {}, {}
        for c in picked.candidates:
            by_template[c.template] = by_template.get(c.template, 0) + 1
            for club in c.club_ids:
                by_club[club] = by_club.get(club, 0) + 1
        assert max(by_template.values()) <= 2 and max(by_club.values()) <= 2, (seed, by_template, by_club)
        assert picked.warnings == []


def test_the_same_seed_gives_the_same_dive_and_another_seed_another():
    pool = pool_of(60)
    assert [c.id for c in pick(pool, seed=3).candidates] == [c.id for c in pick(pool, seed=3).candidates]
    assert {tuple(c.id for c in pick(pool, seed=s).candidates) for s in range(6)} != {
        tuple(c.id for c in pick(pool, seed=3).candidates)}
    # the order of the pool does not matter
    assert [c.id for c in pick(list(reversed(pool)), seed=3).candidates] == [c.id for c in pick(pool, seed=3).candidates]


def test_an_unmeetable_quota_is_relaxed_first_with_a_warning():
    # the hard third all name the same club: with 2 per club only 2... so cap 1 makes the hard quota of 2 impossible
    pool = [Candidate(i, f"t{i % 5}", (99,) if i > 40 else (i,), 1500.0 + i) for i in range(1, 61)]
    rules = replace(RULES, max_per_club=1)
    picked = pick(pool, rules)
    assert len(picked.candidates) == 7
    [warning] = picked.warnings
    assert warning.startswith("quota relaxed: ") and "instead of 2/3/2" in warning
    assert sum(1 for c in picked.candidates if 99 in c.club_ids) == 1  # the cap still holds
    assert [c.seed for c in picked.candidates] == sorted(c.seed for c in picked.candidates)


def test_the_relaxed_quota_stays_as_close_as_possible():
    pool = [Candidate(i, f"t{i % 5}", (99,) if i > 40 else (i,), 1500.0 + i) for i in range(1, 61)]
    picked = pick(pool, replace(RULES, max_per_club=1))
    counts = [sum(b == band for b in picked.bands.values()) for band in ("easy", "medium", "hard")]
    assert counts[2] == 1 and counts[0] + counts[1] == 6  # one hard (the cap), and no band overfilled beyond need


def test_caps_are_relaxed_only_after_the_quota_and_only_as_far_as_needed():
    pool = pool_of(10, templates=["only"])  # every prompt has one template: two per template cannot make 7
    picked = pick(pool)
    assert len(picked.candidates) == 7
    # the club cap (2 per club, every prompt a different club here) is not touched, only the template cap
    assert picked.warnings == ["template cap relaxed: up to 7 per template (wanted 2)"]
    # a pool that needs only one more per template lifts the cap by one, not to seven
    pool = pool_of(30, templates=["x", "y", "z"])  # 10 each: 7 prompts over 3 templates need up to 3 per template
    picked = pick(pool)
    assert picked.warnings == ["template cap relaxed: up to 3 per template (wanted 2)"]
    counts = {t: sum(c.template == t for c in picked.candidates) for t in "xyz"}
    assert max(counts.values()) == 3


def test_never_fewer_than_seven():
    assert pick(pool_of(6)) is None
    assert pick([]) is None
    only = pick(pool_of(7))
    assert only is not None and {c.id for c in only.candidates} == set(range(1, 8))
    # exactly 7 all of one template and club: every rule is broken, the dive is still 7
    awful = pick([Candidate(i, "same", (1,), 1500.0 + i) for i in range(1, 8)])
    assert len(awful.candidates) == 7
    assert "template cap relaxed: up to 7 per template (wanted 2)" in awful.warnings
    assert "club cap relaxed: up to 7 per club (wanted 2)" in awful.warnings


def test_a_pool_of_exactly_one_valid_choice_is_found():
    # 8 prompts, but the cap of 2 per template leaves only one set of 7: drop one of the three "a" prompts
    pool = [Candidate(1, "a", (1,), 1501.0), Candidate(2, "a", (2,), 1502.0), Candidate(3, "a", (3,), 1503.0),
            Candidate(4, "b", (4,), 1504.0), Candidate(5, "b", (5,), 1505.0), Candidate(6, "c", (6,), 1506.0),
            Candidate(7, "c", (7,), 1507.0), Candidate(8, "d", (8,), 1508.0)]
    picked = pick(pool, replace(RULES, quotas={"easy": 2, "medium": 3, "hard": 2}))
    assert len(picked.candidates) == 7
    assert ATTEMPTS >= 100  # a random search that is allowed enough tries


def test_quotas_must_add_up_and_the_test_range_must_be_virtual():
    bad = {**TUNABLES, "scheduler": {**TUNABLES["scheduler"], "easy_quota": 3}}
    with pytest.raises(ValueError, match="quotas add up to 8"):
        DiveRules.from_tunables(bad)
    late = {**TUNABLES, "dive": {**TUNABLES["dive"], "test_date_end": "2000-01-01"}}
    with pytest.raises(ValueError, match="before 2000-01-01"):
        DiveRules.from_tunables(late)


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------

def test_a_dive_runs_from_reset_to_reset_in_the_reset_time_zone():
    opens, closes = window(date(2026, 11, 12), RULES)
    assert (opens.astimezone(UTC), closes.astimezone(UTC)) == (
        datetime(2026, 11, 12, 5, 0, tzinfo=UTC), datetime(2026, 11, 13, 5, 0, tzinfo=UTC))  # EST is UTC-5


def test_a_dive_is_25_hours_when_the_clocks_go_back_and_23_when_they_go_forward():
    opens, closes = window(date(2026, 11, 1), RULES)  # US clocks go back on 2026-11-01
    assert closes.astimezone(UTC) - opens.astimezone(UTC) == timedelta(hours=25)
    opens, closes = window(date(2026, 3, 8), RULES)  # and forward on 2026-03-08
    assert closes.astimezone(UTC) - opens.astimezone(UTC) == timedelta(hours=23)


def test_the_current_dive_date_follows_the_reset_time():
    assert dive_date_at(datetime(2026, 11, 12, 4, 59, tzinfo=UTC), RULES) == date(2026, 11, 11)  # 23:59 EST
    assert dive_date_at(datetime(2026, 11, 12, 5, 0, tzinfo=UTC), RULES) == date(2026, 11, 12)
    later = replace(RULES, reset_time=time(6, 0))
    assert dive_date_at(datetime(2026, 11, 12, 10, 59, tzinfo=UTC), later) == date(2026, 11, 11)  # 05:59 EST
    assert dive_date_at(datetime(2026, 11, 12, 11, 0, tzinfo=UTC), later) == date(2026, 11, 12)


def test_the_next_test_date_is_the_first_free_one_in_the_reserved_range():
    assert next_test_date(set(), RULES) == date(1900, 1, 1)
    assert next_test_date({date(1900, 1, 1), date(1900, 1, 2)}, RULES) == date(1900, 1, 3)
    assert next_test_date({date(1900, 1, 2)}, RULES) == date(1900, 1, 1)
    tiny = replace(RULES, test_date_end=date(1900, 1, 2))
    assert next_test_date({date(1900, 1, 1), date(1900, 1, 2)}, tiny) is None
    assert RULES.test_date_end < date(2000, 1, 1)
