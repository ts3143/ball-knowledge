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

from prompts.gates import PASSED_STATUSES
from prompts.templates import TEMPLATES

from .calibrate import (
    all_group_stats,
    gut_report,
    gut_sweep,
    load_aliases,
    parse_sweep,
    print_gut_sweep,
    print_summary,
    rate_all,
    read_gut,
    sweep,
)
from .features import AnswerSet, Features, compute_features, load_evidence
from .model import ModelParams

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
                 limit: int | None = None, all_statuses: bool = False, n_range: tuple[int, int] | None = None,
                 ids: list[int] | None = None) -> list[StoredPrompt]:
    """Stored prompts (those that passed the gates, approved or rejected included, unless all_statuses) with their
    answers; `limit` is per template. n_range
    keeps prompts with that many answers (inclusive) whatever their status, and ids picks prompts by id.
    `cur` must be a dict_row cursor."""
    lo, hi = n_range or (0, 0)
    cur.execute("select id, template, text, club_ids from prompts"
                " where (%s or %s or status = any(%s)) and (%s::text[] is null or template = any(%s))"
                " and (%s::bigint is null or id = %s) and (%s::bigint[] is null or id = any(%s))"
                " and (%s or (select count(*) from prompt_answers a where a.prompt_id = prompts.id) between %s and %s)"
                " order by template, id",
                (all_statuses, n_range is not None, list(PASSED_STATUSES), template_ids, template_ids, prompt_id,
                 prompt_id, ids, ids,
                 n_range is None, lo, hi))
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


def print_prompt(p: StoredPrompt, features: dict[int, Features], rated, mp: ModelParams, rows: int | None) -> None:
    g = rated.gate
    print(f"\n[{p.id}] {p.template}: {p.text}")
    print(f"  {len(features)} answers · tiers {'/'.join(map(str, g['tier_counts']))} · top {100 * g['top_share']:.1f}% · "
          f"n_eff {g['n_eff']:.1f} · expected {rated.expected_points:.1f} pts · tier gate "
          f"{'pass' if g['pass'] else 'FAIL (' + '; '.join(g['reasons']) + ')'}")
    print(f"  {'tier':<13} {'pts':>3} {'share':>6}  {'name':<32} {'views de':>9} {'views en':>9} {'tie':>7} "
          f"{'since':>6}  flags")
    ordered = sorted(features, key=lambda e: (-rated.scores[e], e))
    for e in ordered[:rows]:
        f = features[e]
        flags = p.flags[e] + feature_flags(f)
        print(f"  {mp.tier_names[rated.tiers[e] - 1]:<13} {rated.points[e]:>3} {100 * rated.shares[e]:>5.1f}%  "
              f"{p.names[e][:32]:<32} {f.views_de:>9} {f.views_en:>9} {f.tie_amount:>7.1f} "
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
            limit: int | None, rows: int | None, summary_only: bool, all_statuses: bool,
            sweep_specs: list[str] | None = None, gut: bool = False, gut_path: Path = GUT_SHEET) -> None:
    mp = ModelParams.from_tunables(tunables)
    grid = parse_sweep(sweep_specs) if sweep_specs else None
    gates = tunables["quality_gates"]
    with conn.cursor(row_factory=dict_row) as cur:
        if gut:
            entries = read_gut(gut_path)
            gut_ids = [e["id"] for e in entries if "id" in e]
            if grid:  # the sweep also needs every size-passing prompt for the tier-gate yield
                prompts = load_prompts(cur, n_range=(gates["min_answers"], gates["max_answers"]))
                have = {p.id for p in prompts}
                prompts += load_prompts(cur, ids=[i for i in gut_ids if i not in have], all_statuses=True)
            else:
                prompts = load_prompts(cur, ids=gut_ids, all_statuses=True)
        else:
            # every size-passing prompt at the lowest alternative minimum, whatever its stored status
            lowest = min(gates["report_min_answers_alternatives"] + [gates["min_answers"]])
            prompts = load_prompts(cur, template_ids, prompt_id, n_range=(lowest, gates["max_answers"]))
        ev = load_evidence(cur, [p.answer_set for p in prompts], competition_qid, tunables)
        aliases = load_aliases(cur, sorted({e for p in prompts for e in p.names})) if gut else {}
    features = {p.id: compute_features(p.answer_set, ev) for p in prompts}
    print(f"{len(prompts)} prompts; reference season {ev.params.reference_season}, "
          f"ref date {ev.params.ref_date}; fame weights {ev.fame_weights}")
    if gut and grid:
        if entries:
            print_gut_sweep(gut_sweep(entries, prompts, features, aliases, tunables, mp, grid), gates["min_answers"])
        else:
            print(gut_report(entries, {}, aliases, mp, gut_path))
        return
    if gut:
        print(gut_report(entries, {r.prompt.id: r for r in rate_all(prompts, features, mp)}, aliases, mp, gut_path))
        return
    rated = rate_all(prompts, features, mp)
    if grid:
        sweep(prompts, features, tunables, mp, grid)
        return
    if not summary_only:
        shown, per_template = [], Counter()
        for r in rated:
            if all_statuses or r.n >= gates["min_answers"]:
                per_template[r.prompt.template] += 1
                if limit is None or per_template[r.prompt.template] <= limit:
                    shown.append(r)
        for r in shown:
            print_prompt(r.prompt, r.features, r.result, mp, rows)
    print_summary(all_group_stats(rated, tunables, mp), tunables, mp)
    by_template: dict[str, list[Features]] = {}
    for r in rated:
        if r.n >= gates["min_answers"]:
            by_template.setdefault(r.prompt.template, []).extend(r.features.values())
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
