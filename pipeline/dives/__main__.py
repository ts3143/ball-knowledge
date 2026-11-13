"""Dive scheduling, run by T. Scheduling is publishing: a scheduled prompt is frozen.

    uv run python -m dives schedule bundesliga                 # fill the next 7 dates from approved prompts
    uv run python -m dives schedule bundesliga --dry-run       # the plan only, nothing written
    uv run python -m dives schedule bundesliga --seed 9
    uv run python -m dives status bundesliga                   # queue and runway
    uv run python -m dives unschedule bundesliga 2026-11-20    # before it opens, with no attempts
    uv run python -m dives review-queue                        # leftover answers waiting for a check (read-only)

    # TEST MODE (local database only): a dive that is open right now, to try the app without waiting for dates.
    uv run python -m dives open-now bundesliga [--minutes 30] [--seed 3]
    # ... and throw test dives away again (their prompts go back to the pool); never touches real dives:
    uv run python -m dives purge-test bundesliga --all [--dry-run] [--yes]
    uv run python -m dives purge-test bundesliga --date 1900-01-02 --date 1900-01-03
"""

import argparse
from datetime import UTC, date, datetime

import psycopg
from psycopg.rows import dict_row

from ingest.config import load_settings

from .plan import (
    DiveError,
    load_competition,
    not_enough_message,
    open_now,
    purge_test,
    schedule,
    unschedule,
)
from .report import print_plan, print_purge, print_review_queue, print_status
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
                    "period. Test dives use up the approved prompts they take until you remove them with "
                    "`purge-test`, which deletes the dive, its attempts and answers and frees the prompts. Test "
                    "data is for the LOCAL database only: never run this against the hosted one.")
    now.add_argument("competition")
    now.add_argument("--minutes", type=int, help="how long it stays open (default: [dive].test_dive_minutes)")
    now.add_argument("--seed", type=int, help="scheduler seed (default: [scheduler].seed)")
    purge = commands.add_parser(
        "purge-test", help="TEST MODE: delete test dives with their attempts and answers (local database only)",
        description="Deletes test dives (virtual dates before 2000) together with their attempts, answers, review "
                    "queue rows, rating history, prompt ratings and the ratings of the players involved, so their "
                    "prompts can be used again. Real dives are refused and real answers stay append-only: the "
                    "database allows this delete only for test dives, from the pipeline's role. An audit row goes "
                    "to test_purges. LOCAL database only.")
    purge.add_argument("competition")
    which = purge.add_mutually_exclusive_group(required=True)
    which.add_argument("--date", type=date.fromisoformat, action="append", help="virtual date of a test dive "
                       "(repeatable)")
    which.add_argument("--all", action="store_true", help="every test dive of the competition")
    purge.add_argument("--dry-run", action="store_true", help="show what would be deleted, delete nothing")
    purge.add_argument("--yes", action="store_true", help="do not ask for confirmation")
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
            elif args.command == "purge-test":
                dates = None if args.all else args.date
                preview = purge_test(conn, comp, dates, dry_run=True)
                print_purge(preview, comp, dry_run=True)
                if args.dry_run:
                    return
                if not args.yes and input("Delete all of this? Type 'yes' to continue: ").strip() != "yes":
                    raise SystemExit("Cancelled; nothing deleted.")
                done = purge_test(conn, comp, dates)
                print(f"Purged {len(done.dive_dates)} test dive(s); {done.counts['prompts_released']} prompts "
                      "are back in the pool.")
            else:
                freed = unschedule(conn, comp, args.date)
                print(f"Removed the {comp.slug} dive of {args.date}; {freed} prompts are free again.")
    except DiveError as e:
        raise SystemExit(str(e)) from e


if __name__ == "__main__":
    main()
