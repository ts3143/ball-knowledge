"""Prompt generation: templates -> queries over the facts -> answer sets -> quality gates.

    uv run python -m prompts generate bundesliga                       # re-runnable
    uv run python -m prompts generate bundesliga --template club_pair
    uv run python -m prompts generate bundesliga --dry-run             # summary only, nothing written
    uv run python -m prompts report                                    # read-only, from the database
    uv run python -m prompts report --write-review                     # also pipeline/data/review/<date>/
    uv run python -m prompts review [--template club_pair] [--limit 20]  # T approves or rejects, one by one
    uv run python -m prompts approve 2801 2802 [--note "..."]          # only gate_passed prompts
    uv run python -m prompts reject 2803 --note "why"
"""

import argparse
import time

import psycopg

from ingest.config import load_settings
from ingest.console import log

from .gates import APPROVED, REJECTED
from .generate import generate, print_summary
from .report import print_report
from .review import ReviewError, competition_id, decide_many, print_counts, review_loop
from .templates import TEMPLATES


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m prompts")
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate", help="generate prompts and answer sets")
    gen.add_argument("competition", choices=["bundesliga"])
    gen.add_argument("--template", choices=list(TEMPLATES), action="append",
                     help="only this template (repeatable)")
    gen.add_argument("--dry-run", action="store_true", help="compute and print the summary, write nothing")
    rep = commands.add_parser("report", help="print yields, samples and checks; read-only")
    rep.add_argument("--write-review", action="store_true", help="also write the review files for T")
    rev = commands.add_parser("review", help="T's approval: show gate_passed prompts one at a time (interactive)")
    rev.add_argument("--template", choices=list(TEMPLATES), action="append", help="only this template (repeatable)")
    rev.add_argument("--limit", type=int, help="review at most this many prompts (templates take turns)")
    for name, helptext in (("approve", "approve gate_passed prompts by id"),
                           ("reject", "reject prompts by id (gate_passed, or approved and not yet scheduled)")):
        cmd = commands.add_parser(name, help=helptext)
        cmd.add_argument("ids", type=int, nargs="+", help="prompt ids")
        cmd.add_argument("--note", help="why" + (" (required)" if name == "reject" else " (optional)"))
    args = parser.parse_args()
    settings = load_settings()
    t0 = time.monotonic()

    if args.command == "review":
        with psycopg.connect(settings.database_url) as conn:
            try:
                comp = competition_id(conn, settings.ingest["competition_qid"])
            except ReviewError as e:
                raise SystemExit(str(e)) from e
            print_counts(review_loop(conn, settings.tunables, comp, args.template, args.limit))
        return

    if args.command in ("approve", "reject"):
        with psycopg.connect(settings.database_url) as conn:
            try:
                decide_many(conn, args.ids, APPROVED if args.command == "approve" else REJECTED, args.note)
            except ReviewError as e:
                raise SystemExit(f"Nothing changed.\n{e}") from e
        print(f"{'approved' if args.command == 'approve' else 'rejected'} {len(set(args.ids))} prompts")
        return

    if args.command == "report":
        with psycopg.connect(settings.database_url) as conn:
            conn.read_only = True
            print_report(conn, settings.ingest["competition_qid"], settings.tunables, args.write_review)
        return

    with psycopg.connect(settings.database_url) as conn:
        stats = generate(conn, settings.ingest["competition_qid"], settings.tunables, args.template, args.dry_run)
    log(f"Done in {time.monotonic() - t0:.0f}s")
    print_summary(stats, args.dry_run)


if __name__ == "__main__":
    main()
