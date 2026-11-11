"""Prompt generation: templates -> queries over the facts -> answer sets -> quality gates.

    uv run python -m prompts generate bundesliga                       # re-runnable
    uv run python -m prompts generate bundesliga --template club_pair
    uv run python -m prompts generate bundesliga --dry-run             # summary only, nothing written
"""

import argparse
import time

import psycopg

from ingest.config import load_settings

from .generate import generate, log, print_summary
from .templates import TEMPLATES


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m prompts")
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate", help="generate prompts and answer sets")
    gen.add_argument("competition", choices=["bundesliga"])
    gen.add_argument("--template", choices=list(TEMPLATES), action="append",
                     help="only this template (repeatable)")
    gen.add_argument("--dry-run", action="store_true", help="compute and print the summary, write nothing")
    args = parser.parse_args()
    settings = load_settings()
    t0 = time.monotonic()

    with psycopg.connect(settings.database_url) as conn:
        stats = generate(conn, settings.ingest["competition_qid"], settings.tunables, args.template, args.dry_run)
    log(f"Done in {time.monotonic() - t0:.0f}s")
    print_summary(stats, args.dry_run)


if __name__ == "__main__":
    main()
