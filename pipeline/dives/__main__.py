"""Dive scheduling, run by T. Scheduling is publishing: a scheduled prompt is frozen.

    uv run python -m dives schedule bundesliga                 # fill the next 7 dates from approved prompts
    uv run python -m dives schedule bundesliga --dry-run       # the plan only, nothing written
    uv run python -m dives schedule bundesliga --seed 9
    uv run python -m dives status bundesliga                   # queue and runway
    uv run python -m dives unschedule bundesliga 2026-11-20    # before it opens, with no attempts
    uv run python -m dives review-queue                        # leftover answers waiting for a check (read-only)

    # TEST MODE (local database only): a dive that is open right now, to try the app without waiting for dates.
    uv run python -m dives open-now bundesliga [--minutes 30] [--seed 3]
"""

import argparse
from datetime import UTC, date, datetime

import psycopg
from psycopg.rows import dict_row

from ingest.config import load_settings

from .plan import DiveError, load_competition, not_enough_message, open_now, schedule, unschedule
from .report import print_plan, print_review_queue, print_status
from .scheduler import DiveRules


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m dives")
    commands = parser.add_subparsers(dest="command", required=True)
    sched = commands.add_parser("schedule", help="fill the missing dates of the next queue_days days")
    sched.add_argument("competition")
    sched.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    sched.add_argument("--seed", type=int, help="scheduler seed (default: [scheduler].seed)")
    status = commands.add_parser("status", help="the queue and the runway of approved prompts")
    status.add_argument("competition")
    unsched = commands.add_parser("unschedule", help="remove a dive that has not opened and has no attempts")
    unsched.add_argument("competition")
    unsched.add_argument("date", type=date.fromisoformat, help="dive date, YYYY-MM-DD")
    rq = commands.add_parser("review-queue", help="leftover answers waiting for a check (read-only)")
    rq.add_argument("--limit", type=int, default=30)
    now = commands.add_parser(
        "open-now", help="TEST MODE: open a test dive right now (local database only)",
        description="Creates a dive that opens now and closes after --minutes, from approved prompts that no dive "
                    "uses, chosen by the same rules as `schedule`. Its date is the next free virtual date before "
                    "2000 (see [dive].test_date_start), so it never collides with a real date and is its own rating "
                    "period. Test dives use up the approved prompts they take, and an attempt in one cannot be "
                    "deleted (answers are append-only). Test data is for the LOCAL database only: never run this "
                    "against the hosted one.")
    now.add_argument("competition")
    now.add_argument("--minutes", type=int, help="how long it stays open (default: [dive].test_dive_minutes)")
    now.add_argument("--seed", type=int, help="scheduler seed (default: [scheduler].seed)")
    args = parser.parse_args()

    settings = load_settings()
    tunables = settings.tunables
    rules = DiveRules.from_tunables(tunables)
    try:
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            if args.command == "review-queue":
                conn.read_only = True
                with conn.cursor() as cur:
                    print_review_queue(cur, args.limit)
                return
            with conn.cursor() as cur:
                comp = load_competition(cur, tunables, args.competition)
            conn.commit()
            seed = getattr(args, "seed", None)
            seed = rules.seed if seed is None else seed
            if args.command == "status":
                conn.read_only = True
                with conn.cursor() as cur:
                    print_status(cur, comp, tunables, rules, datetime.now(UTC))
            elif args.command == "schedule":
                plan = schedule(conn, comp, tunables, rules, seed, args.dry_run)
                if not plan.dives:
                    wanted = len(plan.unscheduled)
                    if not wanted:
                        print(f"Nothing to schedule: the next {rules.queue_days} dates already have dives.")
                        return
                    raise SystemExit(not_enough_message(comp, plan.pool_left, rules, wanted))
                print_plan(plan, comp, rules, seed, args.dry_run)
            elif args.command == "open-now":
                plan = open_now(conn, comp, tunables, rules, args.minutes, seed)
                print_plan(plan, comp, rules, seed, False)
                print("Test dive is open. Test data is for the local database only.")
            else:
                freed = unschedule(conn, comp, args.date)
                print(f"Removed the {comp.slug} dive of {args.date}; {freed} prompts are free again.")
    except DiveError as e:
        raise SystemExit(str(e)) from e


if __name__ == "__main__":
    main()
