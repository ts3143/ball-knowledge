"""Read-only report on the stored prompts: yields and gate failures per template, a seeded sample,
a name check against Wikipedia titles, weak-evidence reasons, and the review files for T.

Everything is read from the database; nothing is generated or written there.
"""

import csv
import json
import random
import re
from collections import Counter
from datetime import date
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from ingest.normalize import normalize

from .generate import current_from_year
from .templates import TEMPLATES

REVIEW_DIR = Path(__file__).resolve().parents[1] / "data" / "review"
NAME_CHECK_LIMIT = 30
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")


def _q(cur, sql: str, params=None) -> list[tuple]:
    cur.execute(sql, params)
    return cur.fetchall()


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

    def __init__(self, cur, quality_gates: dict, current_from_year: int):
        self.qg = quality_gates
        self.prompts = [dict(zip(("id", "template", "params", "text", "status", "answer_count", "gate_results"), r))
                        for r in _q(cur, "select id, template, params, text, status, answer_count, gate_results"
                                         " from prompts order by template, id")]
        by_id = {p["id"]: p for p in self.prompts}
        for p in self.prompts:
            p["answers"] = []
        for r in _q(cur,
                    "select a.prompt_id, a.entity_id, e.canonical_name, a.flags, a.evidence_fact_ids,"
                    " coalesce((select sum(v.views) from entity_pageviews v where v.entity_id = e.id), 0)::bigint"
                    " from prompt_answers a join entities e on e.id = a.entity_id"
                    " order by a.prompt_id, a.entity_id"):
            by_id[r[0]]["answers"].append(dict(zip(("entity_id", "name", "flags", "fact_ids", "views"), r[1:])))
        for p in self.prompts:
            p["answers"].sort(key=lambda a: (-a["views"], a["name"]))

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


def template_numbers(data: Data, template_id: str) -> dict:
    prompts, passing = data.of(template_id), data.passing(template_id)
    gr = [p["gate_results"] for p in prompts]
    answers = [a for p in passing for a in p["answers"]]
    maximum = data.qg["max_answers"]
    return {
        "generated": len(prompts), "passed": len(passing),
        "too_few": sum(g["size"]["n"] < g["size"]["min"] for g in gr),
        "too_many": sum(g["size"]["n"] > g["size"]["max"] for g in gr),
        "too_few_obvious": sum(g["mix"]["obvious"] < g["mix"]["min_obvious"] for g in gr),
        "too_few_obscure": sum(g["mix"]["obscure"] < g["mix"]["min_obscure"] for g in gr),
        "size_passed": sum(g["size"]["pass"] for g in gr),
        "yield": {alt: sum(alt <= p["answer_count"] <= maximum and p["gate_results"]["mix"]["pass"]
                           for p in prompts) for alt in data.qg["report_min_answers_alternatives"]},
        "answers": len(answers),
        "weak": sum("weak_stint" in a["flags"] for a in answers),
        "edge": sum("edge_season" in a["flags"] for a in answers),
    }


def pct(n: int, total: int) -> str:
    return f"{100 * n / total:.1f}%" if total else "-"


def format_numbers(n: dict) -> list[str]:
    return [
        (f"generated {n['generated']}   gate_passed {n['passed']}   failed: too few {n['too_few']}, "
         f"too many {n['too_many']}, too few obvious {n['too_few_obvious']}, too few obscure {n['too_few_obscure']}"
         "   (a prompt can fail several)"),
        f"passed size {n['size_passed']}, of which mix removed {n['size_passed'] - n['passed']}",
        "yield (size and mix) at min answers  " + "   ".join(f"{m}: {c}" for m, c in n["yield"].items()),
        (f"answers in passing prompts {n['answers']}   weak_stint {n['weak']} ({pct(n['weak'], n['answers'])})"
         f"   edge_season {n['edge']} ({pct(n['edge'], n['answers'])})"),
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


def views_line(answers: list[dict]) -> str:
    return ", ".join(f"{a['name']} {a['views']:,}" for a in answers)


def print_report(conn: psycopg.Connection, competition_qid: str, tunables: dict, write_review: bool) -> None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select id from entities where wikidata_id = %s and type = 'competition'", (competition_qid,))
        comp = cur.fetchone()
        if comp is None:
            raise SystemExit(f"No competition entity {competition_qid}; run the ingest first.")
        from_year = current_from_year(cur, comp["id"], competition_qid, tunables)
    with conn.cursor() as cur:
        data = Data(cur, tunables["quality_gates"], from_year)
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
            print("  most viewed:  " + views_line(p["answers"][:5]))
            print("  least viewed: " + views_line(p["answers"][-5:][::-1]))
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


def write_review_files(data: Data, numbers: dict, today: date | None = None) -> Path:
    out = REVIEW_DIR / (today or date.today()).isoformat()
    out.mkdir(parents=True, exist_ok=True)
    for template_id in TEMPLATES:
        lines = [f"# {template_id}", ""]
        for p in data.passing(template_id):
            lines += [f"## {p['text']}", "", f"{p['answer_count']} answers", "",
                      "| name | views | flags | evidence |", "|---|---:|---|---|"]
            for a in p["answers"]:
                # Duplicate facts (e.g. two identical Wikidata statements) render once.
                evidence = "; ".join(dict.fromkeys(render_evidence(data.facts[f]) for f in a["fact_ids"]
                                                   if f in data.facts))
                lines.append(f"| {_cell(a['name'])} | {a['views']:,} | {', '.join(a['flags'])} | {_cell(evidence)} |")
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
        w.writerow(["template", "params", "text", "status", "answer_count", "gate_results"])
        for p in data.prompts:
            w.writerow([p["template"], json.dumps(p["params"], sort_keys=True), p["text"], p["status"],
                        p["answer_count"], json.dumps(p["gate_results"], sort_keys=True, separators=(",", ":"))])
    return out


def _cell(text: str) -> str:
    return text.replace("|", "\\|")
