"""The rules of a dive, as pure functions: which 7 prompts, in which order, and when a dive opens and closes.

A dive takes prompts from a pool of approved, unused prompts:
  * the pool is cut into terciles by seed difficulty and a dive takes a quota from each (2 easy, 3 medium, 2 hard);
  * at most `max_per_template` prompts of one template and at most `max_per_club` naming the same club;
  * the prompts run easiest to hardest.
When the rules cannot all be met, the quota is relaxed first (still keeping the caps), then the caps (one more
per step), each with a warning. A dive never has fewer than `prompts_per_dive` prompts: with fewer left in the
pool there is no dive.
"""

import random
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from prompts.difficulty import BANDS, tercile_bands

# Random greedy attempts per relaxation level. Small pools are solved in the first few; this only bounds the
# search when a level is infeasible.
ATTEMPTS = 300
TEST_DATE_LIMIT = date(2000, 1, 1)  # the database keeps test dives below it and real ones from it


@dataclass(frozen=True)
class Candidate:
    id: int
    template: str
    club_ids: tuple[int, ...]
    seed: float  # seed difficulty, higher = harder


@dataclass(frozen=True)
class Picked:
    candidates: list[Candidate]  # in play order, easiest first
    bands: dict[int, str]  # prompt id -> band (relative to the pool the dive was drawn from)
    warnings: list[str]


@dataclass(frozen=True)
class DiveRules:
    prompts_per_dive: int
    quotas: dict[str, int]  # band -> prompts
    max_per_template: int
    max_per_club: int
    seed: int
    queue_days: int
    reset_timezone: ZoneInfo
    reset_time: time
    test_dive_minutes: int
    test_date_start: date
    test_date_end: date

    @classmethod
    def from_tunables(cls, tunables: dict) -> "DiveRules":
        d, s = tunables["dive"], tunables["scheduler"]
        rules = cls(
            prompts_per_dive=d["prompts_per_dive"],
            quotas={"easy": s["easy_quota"], "medium": s["medium_quota"], "hard": s["hard_quota"]},
            max_per_template=s["max_per_template"], max_per_club=s["max_per_club"], seed=s["seed"],
            queue_days=d["queue_days"], reset_timezone=ZoneInfo(d["reset_timezone"]),
            reset_time=time.fromisoformat(d["reset_time"]), test_dive_minutes=d["test_dive_minutes"],
            test_date_start=date.fromisoformat(d["test_date_start"]),
            test_date_end=date.fromisoformat(d["test_date_end"]))
        if sum(rules.quotas.values()) != rules.prompts_per_dive:
            raise ValueError(f"[scheduler] quotas add up to {sum(rules.quotas.values())}, "
                             f"not [dive].prompts_per_dive = {rules.prompts_per_dive}")
        if not rules.test_date_start <= rules.test_date_end < TEST_DATE_LIMIT:
            raise ValueError("[dive].test_date_start..test_date_end must be an ascending range before 2000-01-01")
        if rules.max_per_template < 1 or rules.max_per_club < 1 or rules.test_dive_minutes < 1:
            raise ValueError("[scheduler] caps and [dive].test_dive_minutes must be at least 1")
        return rules


# ---------------------------------------------------------------------------
# Choosing prompts
# ---------------------------------------------------------------------------

def _attempt(rng: random.Random, by_band: dict[str, list[Candidate]], quotas: dict[str, int],
             n: int, max_template: int, max_club: int) -> tuple[list[Candidate], dict[str, int]]:
    """One random greedy pass: first fill each band's quota (as far as the caps allow), then fill up to n from
    any band. Returns the picks (possibly fewer than n) and the shortfall per band against the quota."""
    templates: Counter = Counter()
    clubs: Counter = Counter()
    picked: list[Candidate] = []
    taken: set[int] = set()

    def take(c: Candidate) -> bool:
        if templates[c.template] >= max_template or any(clubs[k] >= max_club for k in c.club_ids):
            return False
        templates[c.template] += 1
        clubs.update(c.club_ids)
        picked.append(c)
        taken.add(c.id)
        return True

    short = {}
    for band in BANDS:
        pool = by_band[band][:]
        rng.shuffle(pool)
        got = 0
        for c in pool:
            if got == quotas[band]:
                break
            got += take(c)
        short[band] = quotas[band] - got
    rest = [c for band in BANDS for c in by_band[band] if c.id not in taken]
    rng.shuffle(rest)
    for c in rest:
        if len(picked) == n:
            break
        take(c)
    return picked, short


def choose_dive(pool: list[Candidate], rules: DiveRules, rng: random.Random) -> Picked | None:
    """The prompts of one dive, or None when the pool has fewer than prompts_per_dive prompts."""
    n = rules.prompts_per_dive
    if len(pool) < n:
        return None
    bands = tercile_bands({c.id: c.seed for c in pool})
    by_band: dict[str, list[Candidate]] = {b: [] for b in BANDS}
    for c in sorted(pool, key=lambda c: (c.seed, c.id)):  # a fixed order, so the seed alone decides the choice
        by_band[bands[c.id]].append(c)
    warnings: list[str] = []
    picked: list[Candidate] | None = None

    # Level 0: quota and caps.
    for _ in range(ATTEMPTS):
        got, short = _attempt(rng, by_band, rules.quotas, n, rules.max_per_template, rules.max_per_club)
        if len(got) == n and not any(short.values()):
            picked = got
            break

    # Then relax the quota (keeping the caps); if even that cannot fill n, lift the caps by as little as possible
    # (the fewest extra slots in all, the template cap first on a tie), the quota still relaxed.
    for total in range(2 * n):
        for t_extra in range(total, -1, -1):
            c_extra = total - t_extra
            t_cap, c_cap = rules.max_per_template + t_extra, rules.max_per_club + c_extra
            best, best_missing = None, 0
            for _ in range(ATTEMPTS):
                got, short = _attempt(rng, by_band, rules.quotas, n, t_cap, c_cap)
                missing = sum(v for v in short.values() if v > 0)
                if len(got) == n and (best is None or missing < best_missing):
                    best, best_missing = got, missing
                    if not missing:
                        break
            if best is None:
                continue
            picked = best
            counts = Counter(bands[c.id] for c in best)
            if best_missing:
                warnings.append(
                    f"quota relaxed: {'/'.join(str(counts[b]) for b in BANDS)} easy/medium/hard instead of "
                    f"{'/'.join(str(rules.quotas[b]) for b in BANDS)}")
            if t_extra:
                warnings.append(f"template cap relaxed: up to {t_cap} per template (wanted {rules.max_per_template})")
            if c_extra:
                warnings.append(f"club cap relaxed: up to {c_cap} per club (wanted {rules.max_per_club})")
            break
        if picked is not None:
            break
    assert picked is not None  # with every cap at n nothing blocks a pick

    ordered = sorted(picked, key=lambda c: (c.seed, c.id))
    return Picked(ordered, {c.id: bands[c.id] for c in ordered}, warnings)


# ---------------------------------------------------------------------------
# Dates and windows
# ---------------------------------------------------------------------------

def dive_date_at(now: datetime, rules: DiveRules) -> date:
    """The dive date that is current at `now`: today's calendar date in the reset time zone, or yesterday's
    while the day's reset time has not come yet."""
    local = now.astimezone(rules.reset_timezone)
    return local.date() if local.time() >= rules.reset_time else local.date() - timedelta(days=1)


def window(day: date, rules: DiveRules) -> tuple[datetime, datetime]:
    """(opens_at, closes_at) of a real dive: from the reset time on `day` to the next day's reset time, in the
    reset time zone (23 or 25 hours across a clock change), as aware datetimes."""
    tz = rules.reset_timezone
    return (datetime.combine(day, rules.reset_time, tzinfo=tz),
            datetime.combine(day + timedelta(days=1), rules.reset_time, tzinfo=tz))


def next_test_date(used: set[date], rules: DiveRules) -> date | None:
    """The smallest unused date of the reserved test range, or None when it is full."""
    day = rules.test_date_start
    while day <= rules.test_date_end:
        if day not in used:
            return day
        day += timedelta(days=1)
    return None
