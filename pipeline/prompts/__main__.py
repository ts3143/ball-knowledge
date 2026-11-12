"""Prompt generation: templates -> queries over the facts -> answer sets -> quality gates.

    uv run python -m prompts generate bundesliga                       # re-runnable
    uv run python -m prompts generate bundesliga --template club_pair
    uv run python -m prompts generate bundesliga --dry-run             # summary only, nothing written
    uv run python -m prompts report                                    # read-only, from the database
    uv run python -m prompts report --write-review                     # also pipeline/data/review/<date>/
"""

import argparse
import time

import psycopg

from ingest.config import load_settings

from .generate import generate, log, print_summary
from .report import print_report
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
    args = parser.parse_args()
    settings = load_settings()
    t0 = time.monotonic()

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
