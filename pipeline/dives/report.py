"""What `python -m dives` prints: plan tables, the queue and runway, and the review queue. Read-only apart from
the plan tables, which describe what a command just wrote (or, in a dry run, would write)."""

from collections import Counter
from datetime import datetime, timedelta

from .plan import Competition, Plan, PlannedDive, load_pool
from .scheduler import DiveRules, dive_date_at

WIDTH = 100
TEXT_WIDTH = 78


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 1] + "…"


def _when(moment: datetime, rules: DiveRules) -> str:
    return moment.astimezone(rules.reset_timezone).strftime("%Y-%m-%d %H:%M %Z")


def format_dive(d: PlannedDive, plan: Plan, rules: DiveRules) -> list[str]:
    label = "TEST dive" if d.is_test else f"{d.day:%a} {d.day}"
    head = f"{label}   opens {_when(d.opens_at, rules)}   closes {_when(d.closes_at, rules)}"
    lines = ["-" * WIDTH, head + (f"   (virtual date {d.day}, local database only)" if d.is_test else "")]
    for pos, c in enumerate(d.picked.candidates, 1):
        lines.append(f"  {pos}  {d.picked.bands[c.id]:<6} {c.seed:>5.0f}  #{c.id:<5} "
                     f"{_clip(plan.texts[c.id], TEXT_WIDTH)}")
    templates = Counter(c.template for c in d.picked.candidates)
    lines.append("  templates: " + ", ".join(f"{t} {n}" for t, n in sorted(templates.items())))
    lines += [f"  WARNING: {w}" for w in d.picked.warnings]
    return lines


def print_plan(plan: Plan, comp: Competition, rules: DiveRules, seed: int, dry_run: bool,
               out=print) -> None:
    """The table of dives: date and window, then position, band, seed difficulty, prompt id and text."""
    title = f"{comp.name}: {len(plan.dives)} dive{'s' if len(plan.dives) != 1 else ''}, scheduler seed {seed}"
    out("=" * WIDTH)
    out(title + ("  (DRY RUN: nothing written)" if dry_run else ""))
    out("seed = starting difficulty rating of the prompt (higher = harder); bands are thirds of the remaining pool")
    for d in plan.dives:
        for line in format_dive(d, plan, rules):
            out(line)
    out("-" * WIDTH)
    for note in plan.notes:
        out(f"NOTE: {note}")
    if plan.unscheduled:
        out(f"NOT SCHEDULED: {len(plan.unscheduled)} date(s), {plan.unscheduled[0]} to {plan.unscheduled[-1]}: "
            f"not enough approved prompts left (a dive needs {rules.prompts_per_dive}).")
    out(f"approved prompts left unused: {plan.pool_left}")


def _state(now: datetime, opens_at: datetime, closes_at: datetime) -> str:
    return "queued" if now < opens_at else "open" if now < closes_at else "closed"


def print_status(cur, comp: Competition, tunables: dict, rules: DiveRules, now: datetime, out=print) -> None:
    """The queue and the runway of approved prompts; test dives are listed apart."""
    today = dive_date_at(now, rules)
    cur.execute("select status, count(*) as n from prompts where competition_id = %s group by status", (comp.id,))
    by_status = {r["status"]: r["n"] for r in cur.fetchall()}
    pool, _, notes = load_pool(cur, comp, tunables)
    cur.execute("select d.id, d.dive_date, d.opens_at, d.closes_at, d.is_test,"
                " (select count(*) from dive_prompts p where p.dive_id = d.id) as prompts,"
                " (select count(*) from attempts a where a.dive_id = d.id) as attempts"
                " from dives d where d.competition_id = %s order by d.dive_date", (comp.id,))
    dives = cur.fetchall()
    real = [d for d in dives if not d["is_test"]]
    test = [d for d in dives if d["is_test"]]
    ahead = [d for d in real if d["dive_date"] >= today]
    queued_days = {d["dive_date"] for d in ahead}
    gaps = [today + timedelta(days=i) for i in range(rules.queue_days)]
    gaps = [g for g in gaps if g not in queued_days]
    n = rules.prompts_per_dive
    extra_dives = len(pool) // n

    out("=" * WIDTH)
    out(f"{comp.name} ({comp.slug})   dive date now {today}   {_when(now, rules)}")
    out(f"prompts: {by_status.get('gate_passed', 0)} gate_passed waiting for review, "
        f"{by_status.get('approved', 0)} approved, {by_status.get('rejected', 0)} rejected")
    out(f"approved and unused: {len(pool)} = {extra_dives} more dive{'s' if extra_dives != 1 else ''} "
        f"of {n} (the rest, {len(pool) % n}, wait for more approvals)")
    out(f"queue: {len(ahead)} dive{'s' if len(ahead) != 1 else ''} from today on; runway "
        f"{len(ahead) + extra_dives} day{'s' if len(ahead) + extra_dives != 1 else ''} (target {rules.queue_days})")
    if gaps:
        out(f"dates without a dive in the next {rules.queue_days} days: {', '.join(map(str, gaps))}")
        out(f"  `python -m dives schedule {comp.slug}` fills them; it needs {n * len(gaps)} unused approved "
            f"prompts, {len(pool)} available")
    for note in notes:
        out(f"NOTE: {note}")
    out("")
    out(f"scheduled dives ({len(real)}; {len(real) - len(ahead)} before today not shown)")
    if ahead:
        out("  date         state    prompts  attempts  opens                      closes")
        for d in ahead:
            out(f"  {d['dive_date']}   {_state(now, d['opens_at'], d['closes_at']):<7}  {d['prompts']:>7}  "
                f"{d['attempts']:>8}  {_when(d['opens_at'], rules):<25}  {_when(d['closes_at'], rules)}")
    else:
        out("  none")
    out("")
    out(f"test dives ({len(test)}; local database only, virtual dates, they use up approved prompts)")
    if test:
        out("  virtual date state    prompts  attempts  opens                      closes")
        for d in test[-10:]:
            out(f"  {d['dive_date']}   {_state(now, d['opens_at'], d['closes_at']):<7}  {d['prompts']:>7}  "
                f"{d['attempts']:>8}  {_when(d['opens_at'], rules):<25}  {_when(d['closes_at'], rules)}")
        if len(test) > 10:
            out(f"  ... {len(test) - 10} older test dives not shown")
    else:
        out("  none")


def print_review_queue(cur, limit: int, out=print) -> None:
    """Leftover answers waiting for a decision (read-only)."""
    cur.execute("select status, count(*) as n from review_queue group by status")
    counts = {r["status"]: r["n"] for r in cur.fetchall()}
    cur.execute("select q.id, q.prompt_id, p.text, q.input_norm, q.sample_raw, q.occurrences, q.status,"
                " q.candidate_entity_ids, q.last_seen_at from review_queue q join prompts p on p.id = q.prompt_id"
                " where q.status <> 'resolved' order by q.occurrences desc, q.last_seen_at desc limit %s", (limit,))
    rows = cur.fetchall()
    out("=" * WIDTH)
    out("review queue: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "empty"))
    if not rows:
        out("nothing waiting")
        return
    names = {}
    ids = list({i for r in rows for i in r["candidate_entity_ids"]})
    if ids:
        cur.execute("select id, canonical_name from entities where id = any(%s)", (ids,))
        names = {r["id"]: r["canonical_name"] for r in cur.fetchall()}
    for r in rows:
        cand = ", ".join(names.get(i, str(i)) for i in r["candidate_entity_ids"][:5])
        out("-" * WIDTH)
        out(f"#{r['id']}  prompt {r['prompt_id']}  x{r['occurrences']}  {r['status']}  "
            f"last seen {r['last_seen_at']:%Y-%m-%d %H:%M}")
        out(f"  {_clip(r['text'], WIDTH - 4)}")
        out(f"  typed: {_clip(repr(r['sample_raw']), 60)}   normalized: {_clip(r['input_norm'], 40)}")
        out(f"  could be: {cand or '-'}")
