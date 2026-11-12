"""The prompt snapshot (`rarity export-sim`) as arrays, and the re-tiering self-check against pipeline/rarity/model.py."""

import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SIM_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = SIM_ROOT / "data"
MODEL_PATH = SIM_ROOT.parent / "pipeline" / "rarity" / "model.py"


class SnapshotError(RuntimeError):
    """The snapshot is corrupt or no longer reproduces under the pipeline's rarity model."""


def canonical(obj) -> str:
    """Must match pipeline/rarity/export_sim.py, which hashes the same text."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def load_model_module():
    """pipeline/rarity/model.py by file path (it is stdlib-only), so the sim re-tiers with the real code."""
    name = "bk_rarity_model"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, MODEL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Snapshot:
    header: dict
    prompts: list[dict]  # as exported, kept for the re-tiering check
    prompt_id: np.ndarray  # (P,)
    template: list[str]  # (P,)
    competition_id: np.ndarray  # (P,)
    club_ids: list[tuple[int, ...]]  # (P,)
    offsets: np.ndarray  # (P+1,) answers of prompt j are offsets[j]:offsets[j+1]
    entity_id: np.ndarray  # (A,)
    score: np.ndarray  # (A,) the stored rarity score
    predicted_share: np.ndarray  # (A,)
    tier: np.ndarray  # (A,) effective tier, 1..5
    points: np.ndarray  # (A,)

    @property
    def n_prompts(self) -> int:
        return len(self.prompt_id)

    def answers(self, j: int) -> slice:
        return slice(int(self.offsets[j]), int(self.offsets[j + 1]))


def from_document(doc: dict) -> Snapshot:
    prompts = doc["prompts"]
    digest = hashlib.sha256(canonical(prompts).encode()).hexdigest()
    if digest != doc["header"]["prompts_sha256"]:
        raise SnapshotError(f"prompts_sha256 mismatch: header {doc['header']['prompts_sha256']}, content {digest}")
    prompts = sorted(prompts, key=lambda p: p["id"])
    sizes = [len(p["answers"]) for p in prompts]
    flat = [a for p in prompts for a in p["answers"]]
    return Snapshot(
        header=doc["header"], prompts=prompts,
        prompt_id=np.array([p["id"] for p in prompts], dtype=np.int64), template=[p["template"] for p in prompts],
        competition_id=np.array([p["competition_id"] for p in prompts], dtype=np.int64),
        club_ids=[tuple(p["club_ids"]) for p in prompts],
        offsets=np.concatenate([[0], np.cumsum(sizes)]).astype(np.int64),
        entity_id=np.array([a["entity_id"] for a in flat], dtype=np.int64),
        score=np.array([a["score"] for a in flat], dtype=np.float64),
        predicted_share=np.array([a["predicted_share"] for a in flat], dtype=np.float64),
        tier=np.array([a["tier"] for a in flat], dtype=np.int64),
        points=np.array([a["points"] for a in flat], dtype=np.float64))


def load_snapshot(path: Path) -> Snapshot:
    return from_document(json.loads(Path(path).read_text()))


def latest_snapshot_path() -> Path:
    found = sorted(DATA_DIR.glob("prompts-*.json"))
    if not found:
        raise SnapshotError(f"no snapshot in {DATA_DIR}; run `uv run python -m rarity export-sim` in pipeline/")
    return found[-1]


def retier_problems(snapshot: Snapshot) -> list[str]:
    """Re-tiers every prompt from its stored scores with its own stored params using the pipeline's model code and
    lists every difference from the stored shares, tiers, points and tier-gate numbers."""
    model = load_model_module()
    problems: list[str] = []
    for p in snapshot.prompts:
        mp = model.ModelParams.from_params(p["rarity"]["params"])
        scores = {a["entity_id"]: a["score"] for a in p["answers"]}
        weak = frozenset(a["entity_id"] for a in p["answers"] if a["weak"])
        hipster = frozenset(a["entity_id"] for a in p["answers"] if a["hipster_pick"])
        shares = model.softmax_shares(scores)
        model_tiers = model.assign_tiers(scores, shares, mp.cumulative_share)
        tiers = model.effective_tiers(model_tiers, weak, mp)
        for a in p["answers"]:
            e = a["entity_id"]
            expected = (shares[e], model_tiers[e], tiers[e], model.answer_points(tiers[e], e in hipster, mp))
            stored = (a["predicted_share"], a["model_tier"], a["tier"], a["points"])
            if expected != stored:
                problems.append(f"[{p['id']}] entity {e}: stored {stored}, re-tiered {expected}")
        gate = model.tier_gate(tiers, shares, mp)
        if (gate["top_share"], gate["n_eff"]) != (p["top_share"], p["n_eff"]):
            problems.append(f"[{p['id']}] tier gate numbers differ from the re-tiered ones")
    return problems


def check_retiering(snapshot: Snapshot) -> int:
    """Aborts (SnapshotError) unless re-tiering reproduces the snapshot exactly. Returns the prompts checked."""
    problems = retier_problems(snapshot)
    if problems:
        raise SnapshotError(f"re-tiering does not reproduce the snapshot ({len(problems)} differences): "
                            + "; ".join(problems[:5]))
    return len(snapshot.prompts)
