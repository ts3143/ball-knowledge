"""The rating engine's commands.

    uv run python -m rating golden            # write tests/glicko2_cases.json
    uv run python -m rating golden --check    # fail (exit 1) if that file is out of date
    uv run python -m rating example           # Glickman's example and a worked replay, in plain language
    uv run python -m rating example --set glicko.tau=0.8
"""

import argparse
import sys

from . import ENGINE_VERSION
from .example import example
from .golden import GOLDEN_PATH, stale_reasons, write
from .params import load_params


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m rating")
    commands = parser.add_subparsers(dest="command", required=True)
    gold = commands.add_parser("golden", help="write or verify the golden vectors the TypeScript port runs")
    gold.add_argument("--check", action="store_true", help="verify instead of writing")
    ex = commands.add_parser("example", help="worked examples in plain language")
    ex.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                    help="override a tunable, e.g. glicko.tau=0.8 (repeatable)")
    args = parser.parse_args()
    if args.command == "golden":
        if args.check:
            reasons = stale_reasons()
            if reasons:
                print(f"{GOLDEN_PATH} is out of date ({len(reasons)} differences), first few:")
                for r in reasons[:10]:
                    print("  " + r)
                print("run: uv run python -m rating golden")
                sys.exit(1)
            print(f"{GOLDEN_PATH.name} is current ({ENGINE_VERSION})")
        else:
            write()
            print(f"wrote {GOLDEN_PATH} ({ENGINE_VERSION})")
    else:
        example(load_params(args.overrides))


if __name__ == "__main__":
    main()
