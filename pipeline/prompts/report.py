"""Read-only report on the stored prompts: yields, tier-gate results and tier spreads per template, a seeded
sample, a name check against Wikipedia titles, weak-evidence reasons, and the review files for T (the
approval material: per-prompt tier tables, summary.md, prompts.csv and answers.csv).

Everything is read from the database; nothing is generated or written there.
"""

import csv
import json
import random
import re
import statistics
from collections import Counter
from datetime import date
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from ingest.normalize import normalize
from ingest.report import _q
from rarity.model import ModelParams, tier_gate

from .generate import current_from_year
from .templates import TEMPLATES

REVIEW_DIR = Path(__file__).resolve().parents[1] / "data" / "review"
NAME_CHECK_LIMIT = 30
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")


def name_mismatch(canonical_name: str, enwiki_title: str) -> bool:
    """True when the label differs from the English Wikipedia title after normalizing case, accents
    and a trailing parenthetical such as " (footballer)"."""
    title = _PARENTHETICAL.sub("", enwiki_title.replace("_", " "))
    return normalize(canonical_name) != normalize(title)


def render_evidence(fact: dict) -> str:
    """'club years apps (source)' for stints, 'predicate object years' for other facts."""
    years = ""
    if fact["start_year"] or fact["end_year"]:
        years = f" {fact['start_year'] or '?'}–{fact['end_year'] or ''}"
    apps = f" {fact['apps']} apps" if fact["apps"] is not None else ""
    if fact["predicate"] in ("played_for", "coached"):
        label = "" if fact["predicate"] == "played_for" else "coach "
        return f"{label}{fact['object_name']}{years}{apps} ({fact['source']})"
    return f"{fact['predicate']} {fact['object_name']}{years} ({fact['source']})"


class Data:
    """Stored prompts and their answers, loaded once."""

    def __init__(self, cur, quality_gates: dict, current_from_year: int, tier_names: list[str]):
        self.qg = quality_gates
        self.tier_names = tier_names
        self.prompts = [dict(zip(("id", "template", "params", "text", "status", "answer_count", "gate_results",
                                  "rarity"), r))
                        for r in _q(cur, "select id, template, params, text, status, answer_count, gate_results,"
                                         " rarity from prompts order by template, id")]
        by_id = {p["id"]: p for p in self.prompts}
        for p in self.prompts:
            p["answers"] = []
        for r in _q(cur,
                    "select a.prompt_id, a.entity_id, e.canonical_name, a.flags, a.evidence_fact_ids,"
                    " a.views_de, a.views_en, a.tie_amount, a.seasons_since, a.predicted_share, a.tier,"
                    " a.model_tier, a.points"
                    " from prompt_answers a join entities e on e.id = a.entity_id"
                    " order by a.prompt_id, a.entity_id"):
            by_id[r[0]]["answers"].append(dict(zip(
                ("entity_id", "name", "flags", "fact_ids", "views_de", "views_en", "tie", "since", "share", "tier",
                 "model_tier", "points"), r[1:])))
        for p in self.prompts:  # most likely pick first; unrated answers (a prompt that failed the size gate) last
            p["answers"].sort(key=lambda a: (-(a["share"] or 0), a["name"]))

        fact_ids = list({f for p in self.prompts if p["status"] == "gate_passed"
                         for a in p["answers"] for f in a["fact_ids"]})
        self.facts = {r[0]: dict(zip(("id", "predicate", "start_year", "end_year", "apps", "source", "object_name"), r))
                      for r in _q(cur, "select f.id, f.predicate, f.start_year, f.end_year, f.apps, f.source,"
                                       " o.canonical_name from facts f join entities o on o.id = f.object_id"
                                       " where f.id = any(%s)", (fact_ids,))}
        self.weak_reasons = {r[0]: r[1] for r in _q(
            cur, "select fact_id, weak_reasons from stints(%s) where fact_id = any(%s)",
            (current_from_year, fact_ids))}
        self.name_mismatches = _q(
            cur, "select e.canonical_name, e.enwiki_title from entities e where e.type = 'person'"
                 " and e.enwiki_title is not null and e.id in (select entity_id from prompt_answers)"
                 " order by e.canonical_name")
        self.name_mismatches = [(n, t) for n, t in self.name_mismatches if name_mismatch(n, t)]

    def passing(self, template_id: str) -> list[dict]:
        return [p for p in self.prompts if p["template"] == template_id and p["status"] == "gate_passed"]

    def of(self, template_id: str) -> list[dict]:
        return [p for p in self.prompts if p["template"] == template_id]


def tier_gate_reason(reason: str) -> str:
    """'Bandwagon too big: 9 of 20 answers (max 6.80)' -> 'Bandwagon too big'."""
    return reason.split(":")[0] if reason.startswith("Bandwagon") else reason


def weak_cap_lost(p: dict) -> bool:
    """True when a size-passing prompt fails the tier gate on its capped tiers but would pass on the model's."""
    gate = p["gate_results"].get("tiers")
    if not isinstance(gate, dict) or gate["pass"] or p["rarity"] is None:
        return False
    answers = p["answers"]
    mp = ModelParams.from_params(p["rarity"]["params"])
    return tier_gate({a["entity_id"]: a["model_tier"] for a in answers},
                     {a["entity_id"]: a["share"] for a in answers}, mp)["pass"]


def template_numbers(data: Data, template_id: str) -> dict:
    prompts, passing = data.of(template_id), data.passing(template_id)
    gr = [p["gate_results"] for p in prompts]
    tiers = [g["tiers"] for g in gr if isinstance(g["tiers"], dict)]
    rated = [p for p in passing if p["rarity"] is not None]
    answers = [a for p in passing for a in p["answers"]]
    deep = [a for a in answers if a["tier"] is not None and a["tier"] >= len(data.tier_names) - 1]
    maximum = data.qg["max_answers"]

    def med(values):
        return statistics.median(values) if values else 0.0

    return {
        "generated": len(prompts), "passed": len(passing),
        "too_few": sum(g["size"]["n"] < g["size"]["min"] for g in gr),
        "too_many": sum(g["size"]["n"] > g["size"]["max"] for g in gr),
        "size_passed": sum(g["size"]["pass"] for g in gr),
        "tier_failed": sum(not t["pass"] for t in tiers),
        "tier_reasons": Counter(tier_gate_reason(r) for t in tiers for r in t["reasons"]),
        "weak_cap_lost": sum(weak_cap_lost(p) for p in prompts),
        "yield": {alt: sum(alt <= p["answer_count"] <= maximum for p in prompts)
                  for alt in data.qg["report_min_answers_alternatives"]},
        "tier_counts": [med([p["gate_results"]["tiers"]["tier_counts"][k] for p in rated])
                        for k in range(len(data.tier_names))],
        "top_share": med([p["gate_results"]["tiers"]["top_share"] for p in rated]),
        "expected_points": med([p["rarity"]["expected_points"] for p in rated]),
        "answers": len(answers),
        "weak": sum("weak_stint" in a["flags"] for a in answers),
        "edge": sum("edge_season" in a["flags"] for a in answers),
        "deep": len(deep), "deep_flagged": sum(bool(a["flags"]) for a in deep),
    }


def pct(n: int, total: int) -> str:
    return f"{100 * n / total:.1f}%" if total else "-"


def format_numbers(n: dict) -> list[str]:
    reasons = ", ".join(f"{r} {c}" for r, c in n["tier_reasons"].most_common()) or "-"
    counts = "/".join(f"{c:g}" for c in n["tier_counts"])
    return [
        (f"generated {n['generated']}   gate_passed {n['passed']}   failed: too few {n['too_few']}, "
         f"too many {n['too_many']}, tier gate {n['tier_failed']}"),
        (f"tier gate failures by reason (a prompt can have several): {reasons}; "
         f"lost to the weak-answer cap: {n['weak_cap_lost']}"),
        "yield (size only) at min answers  " + "   ".join(f"{m}: {c}" for m, c in n["yield"].items()),
        (f"median over gate_passed: tiers {counts} (Bandwagon/Casual/Real One/Deep Cut/You Know Ball), "
         f"top share {100 * n['top_share']:.1f}%, expected {n['expected_points']:.1f} pts"),
        (f"answers in passing prompts {n['answers']}   weak_stint {n['weak']} ({pct(n['weak'], n['answers'])})"
         f"   edge_season {n['edge']} ({pct(n['edge'], n['answers'])})"),
        f"weak or edge-flagged answers in Deep Cut or You Know Ball: {n['deep_flagged']} of {n['deep']}",
    ]


def weak_breakdown(data: Data) -> tuple[int, Counter]:
    """Answers flagged weak_stint in passing prompts, and the weak reasons of their evidence stints."""
    total, reasons = 0, Counter()
    for p in data.prompts:
        if p["status"] != "gate_passed":
            continue
        for a in p["answers"]:
            if "weak_stint" in a["flags"]:
                total += 1
                for f in a["fact_ids"]:
                    reasons.update(data.weak_reasons.get(f, []))
    return total, reasons


def picks_line(data: Data, answers: list[dict]) -> str:
    return ", ".join(f"{a['name']} ({data.tier_names[a['tier'] - 1]}, {100 * a['share']:.1f}%)" for a in answers)


def print_report(conn: psycopg.Connection, competition_qid: str, tunables: dict, write_review: bool) -> None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select id from entities where wikidata_id = %s and type = 'competition'", (competition_qid,))
        comp = cur.fetchone()
        if comp is None:
            raise SystemExit(f"No competition entity {competition_qid}; run the ingest first.")
        from_year = current_from_year(cur, comp["id"], competition_qid, tunables)
    with conn.cursor() as cur:
        data = Data(cur, tunables["quality_gates"], from_year, tunables["tiers"]["names"])
    qg = tunables["quality_gates"]
    rng = random.Random(qg["report_sample_seed"])

    print("=" * 78 + "\nPROMPTS PER TEMPLATE\n" + "=" * 78)
    numbers = {}
    for template_id in TEMPLATES:
        numbers[template_id] = template_numbers(data, template_id)
        print(f"\n{template_id}")
        for line in format_numbers(numbers[template_id]):
            print("  " + line)

    print("\n" + "=" * 78 + "\nSAMPLE OF PASSING PROMPTS\n" + "=" * 78)
    for template_id in TEMPLATES:
        passing = data.passing(template_id)
        for p in rng.sample(passing, min(qg["report_sample_per_template"], len(passing))):
            print(f"\n[{template_id}] {p['text']}   ({p['answer_count']} answers)")
            print("  " + prompt_header(p))
            print("  most likely:  " + picks_line(data, p["answers"][:5]))
            print("  least likely: " + picks_line(data, p["answers"][-5:][::-1]))
            flagged = [f"{a['name']} ({', '.join(a['flags'])})" for a in p["answers"] if a["flags"]]
            print(f"  flagged ({len(flagged)}): " + ("; ".join(flagged[:10]) + (" ..." if len(flagged) > 10 else "")
                                                     if flagged else "-"))

    print("\n" + "=" * 78 + "\nNAME CHECK (label differs from enwiki title)\n" + "=" * 78)
    print(f"{len(data.name_mismatches)} persons in answer sets; first {NAME_CHECK_LIMIT}:")
    for name, title in data.name_mismatches[:NAME_CHECK_LIMIT]:
        print(f"  {name!r}  vs enwiki {title!r}")

    total, reasons = weak_breakdown(data)
    print("\n" + "=" * 78 + "\nWEAK EVIDENCE (passing prompts)\n" + "=" * 78)
    print(f"weak_stint answers {total}; reasons across their evidence stints: "
          + (", ".join(f"{r} {n}" for r, n in reasons.most_common()) or "-"))

    if write_review:
        out = write_review_files(data, numbers)
        print(f"\nReview files written to {out}")


def prompt_header(p: dict) -> str:
    """'12 answers · tiers 2/2/3/2/3 · top 33% · expected 27.0 pts · tier gate pass' (or fail with the reasons)."""
    gate = p["gate_results"]["tiers"]
    if not isinstance(gate, dict):
        return f"{p['answer_count']} answers · not rated (failed the size gate)"
    verdict = "pass" if gate["pass"] else f"FAIL ({'; '.join(gate['reasons'])})"
    return (f"{p['answer_count']} answers · tiers {'/'.join(map(str, gate['tier_counts']))} · "
            f"top {100 * gate['top_share']:.0f}% · expected {p['rarity']['expected_points']:.1f} pts · "
            f"tier gate {verdict}")


def points_cell(a: dict, tier_names: list[str]) -> str:
    """The points, with a note when the weak-answer cap lowered the tier."""
    if a["tier"] != a["model_tier"]:
        return f"{a['points']} (capped from {tier_names[a['model_tier'] - 1]})"
    return str(a["points"])


def evidence_text(data: Data, a: dict) -> str:
    # Duplicate facts (e.g. two identical Wikidata statements) render once.
    return "; ".join(dict.fromkeys(render_evidence(data.facts[f]) for f in a["fact_ids"] if f in data.facts))


def write_review_files(data: Data, numbers: dict, review_dir: Path = REVIEW_DIR, today: date | None = None) -> Path:
    out = review_dir / (today or date.today()).isoformat()
    out.mkdir(parents=True, exist_ok=True)
    names = data.tier_names
    for template_id in TEMPLATES:
        lines = [f"# {template_id}", ""]
        for p in data.passing(template_id):
            lines += [f"## {p['text']}", "", prompt_header(p), "",
                      "| tier | points | share | name | de views | en views | tie | seasons since | flags | evidence |",
                      "|---|---|---:|---|---:|---:|---:|---:|---|---|"]
            for a in p["answers"]:
                lines.append(
                    f"| {names[a['tier'] - 1]} | {points_cell(a, names)} | {100 * a['share']:.1f}% | {_cell(a['name'])}"
                    f" | {a['views_de']:,} | {a['views_en']:,} | {a['tie']:.1f} | {a['since']:.1f}"
                    f" | {', '.join(a['flags'])} | {_cell(evidence_text(data, a))} |")
            lines.append("")
        (out / f"{template_id}.md").write_text("\n".join(lines))

    total, reasons = weak_breakdown(data)
    lines = ["# Prompt summary", ""]
    for template_id, n in numbers.items():
        lines += [f"## {template_id}", ""] + [f"- {line}" for line in format_numbers(n)] + [""]
    lines += ["## Weak evidence", "",
              f"{total} weak_stint answers in passing prompts; reasons: "
              + (", ".join(f"{r} {n}" for r, n in reasons.most_common()) or "-"), "",
              f"## Name check ({len(data.name_mismatches)} persons whose label differs from the enwiki title)", "",
              "| label | enwiki title |", "|---|---|"]
    lines += [f"| {_cell(n)} | {_cell(t)} |" for n, t in data.name_mismatches]
    (out / "summary.md").write_text("\n".join(lines) + "\n")

    with (out / "prompts.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["template", "params", "text", "status", "answer_count", "model_version", "tier_counts",
                    "top_share", "n_eff", "expected_points", "tier_gate_pass", "tier_gate_reasons", "gate_results"])
        for p in data.prompts:
            gate, rarity = p["gate_results"]["tiers"], p["rarity"]
            rated = isinstance(gate, dict)
            w.writerow([p["template"], json.dumps(p["params"], sort_keys=True), p["text"], p["status"],
                        p["answer_count"], rarity["model_version"] if rarity else "",
                        "/".join(map(str, gate["tier_counts"])) if rated else "", gate["top_share"] if rated else "",
                        gate["n_eff"] if rated else "", rarity["expected_points"] if rarity else "",
                        gate["pass"] if rated else "", "; ".join(gate["reasons"]) if rated else "",
                        json.dumps(p["gate_results"], sort_keys=True, separators=(",", ":"))])

    with (out / "answers.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["prompt_id", "template", "prompt", "entity_id", "name", "tier", "tier_name", "model_tier",
                    "points", "share", "views_de", "views_en", "tie_amount", "seasons_since", "flags", "evidence"])
        for p in data.prompts:
            if p["status"] != "gate_passed":
                continue
            for a in p["answers"]:
                w.writerow([p["id"], p["template"], p["text"], a["entity_id"], a["name"], a["tier"],
                            names[a["tier"] - 1], a["model_tier"], a["points"], a["share"], a["views_de"],
                            a["views_en"], a["tie"], a["since"], ";".join(a["flags"]), evidence_text(data, a)])
    return out


def _cell(text: str) -> str:
    return text.replace("|", "\\|")
