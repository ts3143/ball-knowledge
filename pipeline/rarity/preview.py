"""Read-only views of the rarity features: per-prompt feature tables, a coverage summary and the
gut-check sheet T fills in. Nothing here writes to the database.
"""

import json
import random
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from psycopg.rows import dict_row

from prompts.templates import TEMPLATES

from .features import AnswerSet, Features, compute_features, load_evidence

CALIBRATION_DIR = Path(__file__).resolve().parents[1] / "data" / "calibration"
GUT_SHEET = CALIBRATION_DIR / "gut.toml"

GUT_HEADER = """\
# Gut check for the rarity model. Fill this in BEFORE you look at any tiers or scores.
#
# Each [[prompt]] below is a real prompt with every valid answer listed in alphabetical order.
# For each prompt, using only your football sense:
#   top    = the 5 answers most fans would name first (the most obvious ones)
#   bottom = the 5 answers only a real fan would know (the least obvious ones)
# Type the names exactly as they appear in the list, in quotes, separated by commas, between the brackets:
#   top = ["Name One", "Name Two", "Name Three", "Name Four", "Name Five"]
# Leave a list empty if you can't judge the prompt. Don't edit anything else.
"""


@dataclass
class StoredPrompt:
    id: int
    template: str
    text: str
    answer_set: AnswerSet
    flags: dict[int, list[str]]
    names: dict[int, str]


def load_prompts(cur, template_ids: list[str] | None = None, prompt_id: int | None = None,
                 limit: int | None = None, all_statuses: bool = False) -> list[StoredPrompt]:
    """Stored prompts (gate_passed unless all_statuses) with their answers; `limit` is per template.
    `cur` must be a dict_row cursor."""
    cur.execute("select id, template, text, club_ids from prompts"
                " where (%s or status = 'gate_passed') and (%s::text[] is null or template = any(%s))"
                " and (%s::bigint is null or id = %s) order by template, id",
                (all_statuses, template_ids, template_ids, prompt_id, prompt_id))
    rows, per_template = [], Counter()
    for r in cur.fetchall():
        per_template[r["template"]] += 1
        if limit is None or per_template[r["template"]] <= limit:
            rows.append(r)
    cur.execute("select a.prompt_id, a.entity_id, a.evidence_fact_ids, a.flags, e.canonical_name"
                " from prompt_answers a join entities e on e.id = a.entity_id"
                " where a.prompt_id = any(%s) order by a.prompt_id, a.entity_id", ([r["id"] for r in rows],))
    answers: dict[int, list[dict]] = {}
    for r in cur.fetchall():
        answers.setdefault(r["prompt_id"], []).append(r)
    return [StoredPrompt(
        id=r["id"], template=r["template"], text=r["text"],
        answer_set=AnswerSet(r["template"], list(r["club_ids"]),
                             {a["entity_id"]: list(a["evidence_fact_ids"]) for a in answers.get(r["id"], [])}),
        flags={a["entity_id"]: list(a["flags"]) for a in answers.get(r["id"], [])},
        names={a["entity_id"]: a["canonical_name"] for a in answers.get(r["id"], [])})
        for r in rows]


def feature_flags(f: Features) -> list[str]:
    return [x for x in (
        "apps~" if f.apps_imputed else "", "recency?" if f.recency_unknown else "",
        f"dup-{f.duplicates_dropped}" if f.duplicates_dropped else "",
        f"edge-{f.edge_seasons}/{f.seasons}" if f.edge_seasons else "") if x]


def print_prompt(p: StoredPrompt, features: dict[int, Features], rows: int | None) -> None:
    print(f"\n[{p.id}] {p.template}: {p.text}  ({len(features)} answers)")
    print(f"  {'name':<32} {'views de':>9} {'views en':>9} {'V':>9} {'tie':>7} {'since':>6}  flags")
    ordered = sorted(features, key=lambda e: (-features[e].fame, p.names[e]))
    for e in ordered[:rows]:
        f = features[e]
        flags = p.flags[e] + feature_flags(f)
        print(f"  {p.names[e][:32]:<32} {f.views_de:>9} {f.views_en:>9} {f.fame:>9.0f} {f.tie_amount:>7.1f} "
              f"{f.seasons_since:>6.1f}  {', '.join(flags)}")
    if rows is not None and len(ordered) > rows:
        print(f"  ... {len(ordered) - rows} more")


def coverage_line(label: str, fs: list[Features]) -> str:
    n = len(fs)
    seasons = sum(f.seasons for f in fs)
    edge_seasons = sum(f.edge_seasons for f in fs)

    def pct(k: int) -> str:
        return f"{k:>6} ({100 * k / n:4.1f}%)" if n else f"{k:>6}"

    edge_share = f"{100 * edge_seasons / seasons:4.1f}% of {seasons} seasons" if seasons else "-"
    return (f"{label:<24} {n:>6}  no views {pct(sum(f.views_de + f.views_en == 0 for f in fs))}  "
            f"apps imputed {pct(sum(f.apps_imputed for f in fs))}  "
            f"recency unknown {pct(sum(f.recency_unknown for f in fs))}  "
            f"dups dropped {sum(f.duplicates_dropped for f in fs)} "
            f"(in {sum(f.duplicates_dropped > 0 for f in fs)} answers)  "
            f"answers with edge seasons {pct(sum(f.edge_seasons > 0 for f in fs))}  edge-weighted {edge_share}")


def print_coverage(by_template: dict[str, list[Features]]) -> None:
    print("\n" + "=" * 78 + "\nCOVERAGE (answers)\n" + "=" * 78)
    for template_id in TEMPLATES:
        if template_id in by_template:
            print(coverage_line(template_id, by_template[template_id]))
    print(coverage_line("all", [f for fs in by_template.values() for f in fs]))


def preview(conn, competition_qid: str, tunables: dict, template_ids: list[str] | None, prompt_id: int | None,
            limit: int | None, rows: int | None, summary_only: bool, all_statuses: bool) -> None:
    with conn.cursor(row_factory=dict_row) as cur:
        prompts = load_prompts(cur, template_ids, prompt_id, limit, all_statuses)
        ev = load_evidence(cur, [p.answer_set for p in prompts], competition_qid, tunables)
    by_template: dict[str, list[Features]] = {}
    print(f"{len(prompts)} prompts; reference season {ev.params.reference_season}, "
          f"ref date {ev.params.ref_date}; fame weights {ev.fame_weights}")
    for p in prompts:
        features = compute_features(p.answer_set, ev)
        by_template.setdefault(p.template, []).extend(features.values())
        if not summary_only:
            print_prompt(p, features, rows)
    print_coverage(by_template)


def select_for_gut_sheet(prompts: list[StoredPrompt], per_template: int, seed: int) -> list[StoredPrompt]:
    """A seeded pick of up to per_template prompts of each template, in template then id order."""
    rng = random.Random(seed)
    picked = []
    for template_id in TEMPLATES:
        pool = sorted((p for p in prompts if p.template == template_id), key=lambda p: p.id)
        picked += sorted(rng.sample(pool, min(per_template, len(pool))), key=lambda p: p.id)
    return picked


def render_gut_sheet(prompts: list[StoredPrompt]) -> str:
    """TOML text; answers alphabetical by canonical name, with no scores, views or tiers."""
    out = [GUT_HEADER]
    for p in prompts:
        names = sorted(p.names.values(), key=lambda n: (n.casefold(), n))
        out += ["", "[[prompt]]", f"id = {p.id}", f"text = {json.dumps(p.text, ensure_ascii=False)}",
                "answers = ["] + [f"  {json.dumps(n, ensure_ascii=False)}," for n in names] + [
                "]", "top = []", "bottom = []"]
    text = "\n".join(out) + "\n"
    tomllib.loads(text)  # the sheet must parse
    return text


def gut_sheet(conn, tunables: dict, force: bool, path: Path = GUT_SHEET) -> Path:
    if path.exists() and not force:
        raise SystemExit(f"{path} exists and may hold T's answers; use --force to overwrite it.")
    cal = tunables["rarity"]["calibration"]
    with conn.cursor(row_factory=dict_row) as cur:
        prompts = load_prompts(cur)
    picked = select_for_gut_sheet(prompts, cal["gut_sheet_prompts_per_template"], cal["gut_sheet_seed"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_gut_sheet(picked))
    return path
