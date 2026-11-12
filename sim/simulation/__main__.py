"""The simulation's commands.

    uv run python -m simulation world                       # world sanity report, model-true, seed 1
    uv run python -m simulation world --world model-wrong --seed 2
    uv run python -m simulation world --set world.players=200 --set tiers.points=[10,25,50,75,100]
"""

import argparse
import time
from pathlib import Path

from .runner import OUTPUT_DIR, world_report
from .snapshot import SnapshotError, latest_snapshot_path
from .world import WORLDS


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m simulation")
    commands = parser.add_subparsers(dest="command", required=True)
    world = commands.add_parser("world", help="write the world sanity report (outputs under sim/output/)")
    world.add_argument("--world", choices=WORLDS, default="model-true")
    world.add_argument("--seed", type=int, default=1)
    world.add_argument("--snapshot", type=Path, help="default: the latest sim/data/prompts-*.json")
    world.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    world.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                       help="override a tunable or a world.* value (repeatable)")
    args = parser.parse_args()
    started = time.time()
    try:
        out = world_report(args.snapshot or latest_snapshot_path(), args.world, args.seed, args.overrides, args.out_dir)
    except SnapshotError as e:
        raise SystemExit(f"snapshot problem: {e}") from e
    print(f"wrote {out} in {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
