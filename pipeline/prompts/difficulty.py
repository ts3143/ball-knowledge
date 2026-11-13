"""A prompt's seed difficulty, from the step-4 rating code, and the difficulty bands built on it.

The seed is the starting Glicko-2 rating of a prompt (higher = harder): sim/rating/seed.py applied to features
frozen in the prompt (rarity.log_recall_mass, gate_results.tiers.n_eff) with the [glicko.prompt_seed] tunables.
That code is stdlib-only and lives in sim/, so it is imported by path instead of being copied a third time (the
TypeScript engine in lib/glicko2 is the second copy). TypeScript writes the real prompt_ratings; the pipeline
uses the seeds only to choose and to show prompts.
"""

import importlib.util
import sys
from collections.abc import Mapping
from functools import lru_cache
from types import ModuleType

from ingest.config import REPO_ROOT

RATING_DIR = REPO_ROOT / "sim" / "rating"
_PACKAGE = "bk_sim_rating"  # sim/rating loaded under this name, so it cannot shadow or clash with another `rating`

BANDS = ("easy", "medium", "hard")


@lru_cache(maxsize=1)
def _rating_modules() -> tuple[ModuleType, ModuleType]:
    """(seed, params) of sim/rating, loaded as the package _PACKAGE."""
    spec = importlib.util.spec_from_file_location(_PACKAGE, RATING_DIR / "__init__.py",
                                                  submodule_search_locations=[str(RATING_DIR)])
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the rating code from {RATING_DIR}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE] = package
    spec.loader.exec_module(package)
    return importlib.import_module(f"{_PACKAGE}.seed"), importlib.import_module(f"{_PACKAGE}.params")


def seed_difficulty(tunables: dict, rarity: Mapping | None, gate_results: Mapping) -> float | None:
    """The seed rating of a prompt row (higher = harder), or None when it lacks the frozen features (a prompt
    that was never rated cannot be seeded)."""
    if rarity is None:
        return None
    seed, params = _rating_modules()
    try:
        return seed.seed_rating(seed.prompt_features(rarity, gate_results),
                                params.Params.from_tunables(tunables).seed)
    except ValueError:
        return None


def tercile_bands(seeds: Mapping[int, float]) -> dict[int, str]:
    """prompt id -> 'easy' | 'medium' | 'hard': the pool cut into thirds by seed, then id. With fewer prompts than
    bands, the lowest bands are used first."""
    ranked = sorted(seeds, key=lambda pid: (seeds[pid], pid))
    n = len(ranked)
    return {pid: BANDS[min(len(BANDS) - 1, i * len(BANDS) // n)] for i, pid in enumerate(ranked)}
