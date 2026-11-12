"""Rarity model tools, read-only.

    uv run python -m rarity preview                       # feature tables for every passing prompt + coverage
    uv run python -m rarity preview --template club_pair --limit 2 --rows 15
    uv run python -m rarity preview --prompt 123
    uv run python -m rarity preview --summary-only        # tier-gate, model and coverage numbers only
    uv run python -m rarity preview --sweep alpha=0.6,0.8,1.0 max_bandwagon_answer_share=0.3,0.34,0.4
    uv run python -m rarity preview --gut                 # compare the model with T's lists in gut.toml
    uv run python -m rarity check                         # stored prompts recompute exactly (exit 1 if not)
    uv run python -m rarity gut-sheet                     # pipeline/data/calibration/gut.toml for T to fill in
    uv run python -m rarity gut-sheet --force             # overwrite an existing sheet
    uv run python -m rarity export-sim                    # read-only snapshot for the simulation: sim/data/prompts-<date>.json
"""

import argparse

import psycopg

from ingest.config import load_settings
from prompts.templates import TEMPLATES

from .check import check
from .export_sim import export_sim
from .preview import gut_sheet, preview


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m rarity")
    commands = parser.add_subparsers(dest="command", required=True)
    prev = commands.add_parser("preview", help="per-prompt tier tables, a tier-gate summary, sweeps and the gut report")
    prev.add_argument("--template", choices=list(TEMPLATES), action="append", help="only this template (repeatable)")
    prev.add_argument("--prompt", type=int, help="only this prompt id")
    prev.add_argument("--limit", type=int, help="at most this many prompts per template")
    prev.add_argument("--rows", type=int, help="print only the top rows of each table (by V)")
    prev.add_argument("--summary-only", action="store_true", help="print only the coverage summary")
    prev.add_argument("--all-statuses", action="store_true", help="show tables for prompts under the minimum answers too")
    prev.add_argument("--sweep", nargs="+", metavar="KEY=V1,V2",
                      help="summary numbers for every combination, e.g. alpha=0.6,0.8 max_bandwagon_answer_share=0.3,0.4")
    prev.add_argument("--gut", action="store_true", help="compare the model with T's top and bottom lists (gut.toml)")
    commands.add_parser("check", help="recompute every stored prompt from its stored features and params")
    commands.add_parser("export-sim", help="write the read-only prompt snapshot the simulation reads (sim/data/)")
    sheet = commands.add_parser("gut-sheet", help="write the sheet T fills in before seeing any tiers")
    sheet.add_argument("--force", action="store_true", help="overwrite an existing gut.toml")
    args = parser.parse_args()
    settings = load_settings()

    with psycopg.connect(settings.database_url) as conn:
        conn.read_only = True
        if args.command == "preview":
            preview(conn, settings.ingest["competition_qid"], settings.tunables, args.template, args.prompt,
                    args.limit, args.rows, args.summary_only, args.all_statuses, args.sweep, args.gut)
        elif args.command == "check":
            raise SystemExit(1 if check(conn) else 0)
        elif args.command == "export-sim":
            export_sim(conn)
        else:
            print(gut_sheet(conn, settings.tunables, args.force))


if __name__ == "__main__":
    main()
