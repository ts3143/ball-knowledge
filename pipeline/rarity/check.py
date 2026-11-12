"""`python -m rarity check`: every stored prompt on the current MODEL_VERSION recomputes, from its stored raw
features and params, to exactly the same score, share, tiers, points and gate. Read-only.
"""

from collections import defaultdict

from psycopg.rows import dict_row

from .model import MODEL_VERSION
from .stage import RARITY_COLUMNS, exact_floats, recompute

_ROUND = 6


def check(conn, competition_id: int | None = None) -> int:
    """Prints the findings; returns the number of mismatches (0 = pass). competition_id limits it to one
    competition's prompts."""
    problems: list[str] = []
    other_versions: dict[str, list[int]] = defaultdict(list)
    checked = answers_checked = 0
    with conn.cursor(row_factory=dict_row) as cur:
        exact_floats(cur)
        cur.execute("select id, rarity, gate_results from prompts"
                    " where %s::bigint is null or competition_id = %s order by id", (competition_id, competition_id))
        prompts = cur.fetchall()
        cur.execute(f"select prompt_id, entity_id, flags, {', '.join(RARITY_COLUMNS)} from prompt_answers"
                    " where score is not null and prompt_id = any(%s) order by prompt_id, entity_id",
                    ([p["id"] for p in prompts],))
        rows: dict[int, list[dict]] = defaultdict(list)
        for r in cur.fetchall():
            rows[r["prompt_id"]].append(r)
    for p in prompts:
        pid, rarity = p["id"], p["rarity"]
        if rarity is None:
            if p["gate_results"]["size"]["pass"]:
                problems.append(f"[{pid}] passes the size gate but has no rarity; run prompts generate")
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
            if rarity[key] != round(value, _ROUND):
                problems.append(f"[{pid}] prompts.rarity.{key} differs from the recomputation")
    print(f"checked {checked} prompts ({answers_checked} answers) on {MODEL_VERSION}: "
          f"{'all reproduce exactly' if not problems else f'{len(problems)} mismatches'}")
    for version, ids in sorted(other_versions.items()):
        print(f"{len(ids)} prompts on {version}, not recomputed: {ids[:10]}{' ...' if len(ids) > 10 else ''}")
    for line in problems:
        print("MISMATCH " + line)
    return len(problems)
