"""`python -m rarity export-sim`: a read-only snapshot of the rated prompts for the simulation (sim/data/, gitignored).
Per prompt: id, template, competition and club entity ids, answer count, the rarity values frozen at approval, and
per answer its stored score, predicted share, tiers and points. No names, QIDs or other facts: entity ids only.
The header's prompts_sha256 is the hash of the canonical JSON of the prompts; reports record it, and the
simulation recomputes it on load.
"""

import hashlib
import json
import subprocess
from collections import defaultdict
from datetime import date
from pathlib import Path

from psycopg.rows import dict_row

from ingest.config import REPO_ROOT

from .model import MODEL_VERSION, WEAK_FLAG
from .stage import exact_floats

SIM_DATA_DIR = REPO_ROOT / "sim" / "data"
STATUSES = ("gate_passed", "approved")
# prompts.rarity.params keys that name a competition (QIDs) or are feature-extraction details: not exported.
# What stays is exactly what re-tiering needs (ModelParams.from_params).
DROPPED_PARAMS = ("fame_weights", "features")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def prompts_sha256(prompts: list[dict]) -> str:
    return hashlib.sha256(canonical(prompts).encode()).hexdigest()


def snapshot_prompt(p: dict, answers: list[dict]) -> dict:
    """One exported prompt from a prompts row (with its answers) as read from the database."""
    rarity = {k: v for k, v in p["rarity"].items() if k != "params"}
    rarity["params"] = {k: v for k, v in p["rarity"]["params"].items() if k not in DROPPED_PARAMS}
    tiers = p["gate_results"]["tiers"]
    return {
        "id": p["id"], "template": p["template"], "status": p["status"], "competition_id": p["competition_id"],
        "club_ids": sorted(p["club_ids"]), "answer_count": p["answer_count"], "rarity": rarity,
        "top_share": tiers["top_share"], "n_eff": tiers["n_eff"],
        "answers": [{"entity_id": a["entity_id"], "score": a["score"], "predicted_share": a["predicted_share"],
                     "model_tier": a["model_tier"], "tier": a["tier"], "points": a["points"],
                     "weak": WEAK_FLAG in a["flags"], "hipster_pick": a["hipster_pick"]}
                    for a in sorted(answers, key=lambda a: a["entity_id"])],
    }


def build_snapshot(prompt_rows: list[dict], answer_rows: list[dict], export_date: str, git_commit: str) -> dict:
    by_prompt: dict[int, list[dict]] = defaultdict(list)
    for a in answer_rows:
        by_prompt[a["prompt_id"]].append(a)
    prompts = [snapshot_prompt(p, by_prompt[p["id"]]) for p in sorted(prompt_rows, key=lambda p: p["id"])]
    return {
        "header": {"export_date": export_date, "git_commit": git_commit, "model_version": MODEL_VERSION,
                   "prompt_count": len(prompts), "answer_count": sum(len(p["answers"]) for p in prompts),
                   "prompts_sha256": prompts_sha256(prompts)},
        "prompts": prompts,
    }


def git_commit() -> str:
    out = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                         text=True, check=False)
    return out.stdout.strip() or "unknown"


def export_sim(conn, out_dir: Path = SIM_DATA_DIR) -> Path:
    with conn.cursor(row_factory=dict_row) as cur:
        exact_floats(cur)  # scores and shares must read back exactly for the re-tiering self-check
        cur.execute("select id, template, status, competition_id, club_ids, answer_count, rarity, gate_results"
                    " from prompts where status = any(%s) and jsonb_typeof(rarity) = 'object' order by id",
                    (list(STATUSES),))
        prompt_rows = cur.fetchall()
        cur.execute("select prompt_id, entity_id, score, predicted_share, model_tier, tier, points, hipster_pick, flags"
                    " from prompt_answers where score is not null and prompt_id = any(%s) order by prompt_id, entity_id",
                    ([p["id"] for p in prompt_rows],))
        answer_rows = cur.fetchall()
    today = date.today()
    snapshot = build_snapshot(prompt_rows, answer_rows, today.isoformat(), git_commit())
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"prompts-{today:%Y%m%d}.json"
    path.write_text(json.dumps(snapshot, sort_keys=True, indent=None, separators=(",", ":")) + "\n")
    print(f"wrote {path}")
    print(json.dumps(snapshot["header"], indent=2))
    return path
