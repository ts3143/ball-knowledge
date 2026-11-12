"""The simulation's commands.

    uv run python -m simulation world                       # world sanity report, model-true, seed 1
    uv run python -m simulation world --world model-wrong --seed 2
    uv run python -m simulation run --world model-true --scenario population
    uv run python -m simulation run --world model-wrong --scenario testers --seed 1 --set glicko.tau=0.8
    uv run python -m simulation fit-seed                     # the seed fit, then both worlds and scenarios before/after
    uv run python -m simulation world --set world.players=200 --set tiers.points=[10,25,50,75,100]
"""

import argparse
import time
from pathlib import Path

from .commands import default_workers, fit_seed_command, run_command
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
    run = commands.add_parser("run", help="replay a scenario through the rating engine and report the metrics")
    run.add_argument("--world", choices=WORLDS, default="model-true")
    run.add_argument("--scenario", choices=("population", "testers"), default="population")
    fit = commands.add_parser("fit-seed", help="fit the prompt seed map, then run both worlds and scenarios with and without it")
    for p in (run, fit):
        p.add_argument("--seed", type=int, default=1, help="first seed; a scenario uses seed, seed + 1, ...")
        p.add_argument("--snapshot", type=Path)
        p.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
        p.add_argument("--workers", type=int, default=default_workers())
        p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                       help="override a tunable, world.* or scenarios.* value (repeatable)")
    args = parser.parse_args()
    started = time.time()
    try:
        if args.command == "run":
            out = run_command(args.snapshot or latest_snapshot_path(), args.world, args.scenario, args.seed, args.overrides,
                              args.out_dir, args.workers)
            print(f"wrote {out} in {time.time() - started:.1f}s")
            return
        if args.command == "fit-seed":
            out, _ = fit_seed_command(args.snapshot or latest_snapshot_path(), args.seed, args.overrides, args.out_dir, args.workers)
            print(f"wrote {out} in {time.time() - started:.1f}s")
            return
        out = world_report(args.snapshot or latest_snapshot_path(), args.world, args.seed, args.overrides, args.out_dir)
    except SnapshotError as e:
        raise SystemExit(f"snapshot problem: {e}") from e
    print(f"wrote {out} in {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
