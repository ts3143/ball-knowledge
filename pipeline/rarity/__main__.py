"""Rarity model tools, read-only.

    uv run python -m rarity preview                       # feature tables for every passing prompt + coverage
    uv run python -m rarity preview --template club_pair --limit 2 --rows 15
    uv run python -m rarity preview --prompt 123
    uv run python -m rarity preview --summary-only        # coverage numbers only
    uv run python -m rarity gut-sheet                     # pipeline/data/calibration/gut.toml for T to fill in
    uv run python -m rarity gut-sheet --force             # overwrite an existing sheet
"""

import argparse

import psycopg

from ingest.config import load_settings
from prompts.templates import TEMPLATES

from .preview import gut_sheet, preview


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m rarity")
    commands = parser.add_subparsers(dest="command", required=True)
    prev = commands.add_parser("preview", help="per-prompt feature tables and a coverage summary")
    prev.add_argument("--template", choices=list(TEMPLATES), action="append", help="only this template (repeatable)")
    prev.add_argument("--prompt", type=int, help="only this prompt id")
    prev.add_argument("--limit", type=int, help="at most this many prompts per template")
    prev.add_argument("--rows", type=int, help="print only the top rows of each table (by V)")
    prev.add_argument("--summary-only", action="store_true", help="print only the coverage summary")
    prev.add_argument("--all-statuses", action="store_true", help="include prompts that did not pass the gates")
    sheet = commands.add_parser("gut-sheet", help="write the sheet T fills in before seeing any tiers")
    sheet.add_argument("--force", action="store_true", help="overwrite an existing gut.toml")
    args = parser.parse_args()
    settings = load_settings()

    with psycopg.connect(settings.database_url) as conn:
        conn.read_only = True
        if args.command == "preview":
            preview(conn, settings.ingest["competition_qid"], settings.tunables, args.template, args.prompt,
                    args.limit, args.rows, args.summary_only, args.all_statuses)
        else:
            print(gut_sheet(conn, settings.tunables, args.force))


if __name__ == "__main__":
    main()
