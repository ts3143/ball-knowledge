"""`python -m rarity check`: stored prompts are consistent, and every prompt on the current MODEL_VERSION
recomputes, from its stored raw features and params, to exactly the same score, share, tiers, points and gate.
Read-only.
"""

from collections import defaultdict

from psycopg.rows import dict_row

from prompts.gates import status as gate_status

from .model import MODEL_VERSION, ROUND_DIGITS
from .stage import RARITY_COLUMNS, exact_floats, recompute


def structure_problems(p: dict, answer_rows: int) -> list[str]:
    """What is inconsistent in one prompt row regardless of model version: p has id, status, gate_results,
    answer_count, rarity (None for SQL NULL) and rarity_type (jsonb_typeof of it)."""
    pid, out = p["id"], []
    if p["status"] != gate_status(p["gate_results"]):
        out.append(f"[{pid}] status {p['status']!r} contradicts its gate_results ({gate_status(p['gate_results'])!r})")
    if p["answer_count"] != answer_rows:
        out.append(f"[{pid}] answer_count {p['answer_count']} but {answer_rows} answer rows")
    size_passed = p["gate_results"]["size"]["pass"]
    if size_passed and p["rarity_type"] != "object":
        out.append(f"[{pid}] passes the size gate but has no rarity; run prompts generate")
    elif not size_passed and p["rarity_type"] is not None:
        out.append(f"[{pid}] fails the size gate but has rarity of type {p['rarity_type']}")
    if p["rarity_type"] == "object":
        for key in ("reference_season", "pageviews_through"):
            if p["rarity"].get(key) is None:
                out.append(f"[{pid}] prompts.rarity has no {key}")
    return out


def check(conn, competition_id: int | None = None) -> int:
    """Prints the findings; returns the number of mismatches (0 = pass). competition_id limits it to one
    competition's prompts."""
    problems: list[str] = []
    other_versions: dict[str, list[int]] = defaultdict(list)
    checked = answers_checked = 0
    with conn.cursor(row_factory=dict_row) as cur:
        exact_floats(cur)
        cur.execute("select id, status, gate_results, answer_count, rarity, jsonb_typeof(rarity) as rarity_type"
                    " from prompts where %s::bigint is null or competition_id = %s order by id",
                    (competition_id, competition_id))
        prompts = cur.fetchall()
        ids = [p["id"] for p in prompts]
        cur.execute("select prompt_id, count(*) as n from prompt_answers where prompt_id = any(%s)"
                    " group by prompt_id", (ids,))
        counts = {r["prompt_id"]: r["n"] for r in cur.fetchall()}
        cur.execute(f"select prompt_id, entity_id, flags, {', '.join(RARITY_COLUMNS)} from prompt_answers"
                    " where score is not null and prompt_id = any(%s) order by prompt_id, entity_id", (ids,))
        rows: dict[int, list[dict]] = defaultdict(list)
        for r in cur.fetchall():
            rows[r["prompt_id"]].append(r)
    for p in prompts:
        pid, rarity = p["id"], p["rarity"]
        problems += structure_problems(p, counts.get(pid, 0))
        if rarity is None:
            if rows[pid]:
                problems.append(f"[{pid}] has rated answers but no prompts.rarity")
            continue
        if rarity["model_version"] != MODEL_VERSION:
            other_versions[rarity["model_version"]].append(pid)
            continue
        checked += 1
        answers_checked += len(rows[pid])
        result, expected = recompute(rarity, rows[pid])
        for r in rows[pid]:
            stored = tuple(r[c] for c in RARITY_COLUMNS)
            if stored != expected[r["entity_id"]]:
                diff = [c for c, a, b in zip(RARITY_COLUMNS, stored, expected[r["entity_id"]]) if a != b]
                problems.append(f"[{pid}] entity {r['entity_id']}: {', '.join(diff)} differ from the recomputation")
        if p["gate_results"].get("tiers") != result.gate:
            problems.append(f"[{pid}] gate_results.tiers differs from the recomputation")
        for key, value in (("expected_points", result.expected_points), ("log_recall_mass", result.log_recall_mass)):
            if rarity[key] != round(value, ROUND_DIGITS):
                problems.append(f"[{pid}] prompts.rarity.{key} differs from the recomputation")
    print(f"checked {checked} prompts ({answers_checked} answers) on {MODEL_VERSION}: "
          f"{'all reproduce exactly' if not problems else f'{len(problems)} mismatches'}")
    for version, version_ids in sorted(other_versions.items()):
        print(f"{len(version_ids)} prompts on {version}, not recomputed: {version_ids[:10]}"
              f"{' ...' if len(version_ids) > 10 else ''}")
    for line in problems:
        print("MISMATCH " + line)
    return len(problems)
