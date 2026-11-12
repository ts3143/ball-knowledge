"""Generates prompts and their answer sets from the facts, one transaction per run.

Prompts are keyed on (template, params). Candidates are rewritten only when something changed;
approved prompts are never rewritten or deleted (hard rule 6): the run reports how their answer
set would differ and leaves them alone.
"""

import json
import sys
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import gates
from .templates import TEMPLATES, Template


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class Generated:
    template: Template
    params: dict
    text: str
    club_ids: list[int]
    answer_query: str
    answers: dict[int, tuple[list[int], list[str]]]  # entity_id -> (evidence_fact_ids, flags)
    gate_results: dict = field(default_factory=dict)  # set by _apply_gates
    status: str = "candidate"

    @property
    def key(self) -> tuple[str, str]:
        return self.template.id, params_key(self.params)


@dataclass
class ApprovedDiff:
    prompt_id: int
    text: str
    added: list[str]
    removed: list[str]


@dataclass
class Stats:
    generated: int = 0
    gate_passed: int = 0
    too_few: int = 0
    too_many: int = 0
    too_few_obvious: int = 0
    too_few_obscure: int = 0
    size_passed: int = 0
    pass_at_min: dict[int, int] = field(default_factory=dict)
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    approved: int = 0
    approved_gone: int = 0
    approved_diffs: list[ApprovedDiff] = field(default_factory=list)


def params_key(params: dict) -> str:
    return json.dumps(params, sort_keys=True)


def current_from_year(cur, competition_id: int, competition_qid: str, tunables: dict) -> int:
    """Open-ended spells from this season on are current, not weak: the latest season in the data
    minus the tunable window (rendered as a literal into each stored answer_query)."""
    cur.execute("select max(season_start_year) as y from club_seasons where competition_id = %s",
                (competition_id,))
    latest_season = cur.fetchone()["y"]  # a dict_row cursor
    if latest_season is None:
        raise SystemExit(f"No seasons for {competition_qid}; run the ingest first.")
    return latest_season - (tunables["prompts"]["open_ended_current_seasons"] - 1)


def generate(conn: psycopg.Connection, competition_qid: str, tunables: dict,
             template_ids: list[str] | None = None, dry_run: bool = False) -> dict[str, Stats]:
    """Runs the templates against the database behind `conn`; returns stats per template id."""
    stored = [TEMPLATES[t] for t in (template_ids or TEMPLATES)]
    with conn.transaction(force_rollback=dry_run), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select id, canonical_name from entities where wikidata_id = %s and type = 'competition'",
                    (competition_qid,))
        comp = cur.fetchone()
        if comp is None:
            raise SystemExit(f"No competition entity {competition_qid}; run the ingest first.")
        cur.execute("select max(last_seen_at) as t from facts")
        facts_as_of = cur.fetchone()["t"]

        ctx = {"competition": competition_qid,
               "teams_per_season": tunables["ingest"]["coverage"]["teams_per_season"],
               "current_from_year": current_from_year(cur, comp["id"], competition_qid, tunables)}
        wording_ctx = {"start_year": tunables["ingest"]["start_year"], "competition": comp["canonical_name"]}
        quality_gates = tunables["quality_gates"]

        # The mix gate's quantiles span every template's answer sets, so all templates are always
        # evaluated; --template only limits what is stored.
        by_template = {}
        for template in TEMPLATES.values():
            log(f"{template.id}: enumerating params")
            by_template[template.id] = _generate_template(conn, cur, template, ctx, wording_ctx)
            log(f"{template.id}: {len(by_template[template.id])} prompts")
        _apply_gates([g for gs in by_template.values() for g in gs], cur, quality_gates)

        return {t.id: _store(cur, t, comp["id"], by_template[t.id], facts_as_of, quality_gates) for t in stored}


def _generate_template(conn, cur, template: Template, ctx: dict, wording_ctx: dict) -> list[Generated]:
    renderer = psycopg.ClientCursor(conn)
    cur.execute(renderer.mogrify(template.params_sql, ctx))
    param_sets = cur.fetchall()

    qids = {p[k] for p in param_sets for k in template.qid_params}
    cur.execute("select wikidata_id, id, canonical_name from entities where wikidata_id = any(%s)", (list(qids),))
    entities = {r["wikidata_id"]: r for r in cur.fetchall()}

    out = []
    for params in param_sets:
        names = {k: entities[params[k]]["canonical_name"] for k in template.qid_params}
        text = template.wording.format(**{**params, **names, **wording_ctx})
        # Rendered with literals, so the stored query reproduces the answer set on its own.
        answer_query = renderer.mogrify(template.answers_sql, {**ctx, **params})
        cur.execute(answer_query)
        answers = {r["entity_id"]: (r["evidence_fact_ids"], r["flags"]) for r in cur.fetchall()}
        out.append(Generated(
            template=template, params=dict(params), text=text,
            club_ids=[entities[params[k]]["id"] for k in template.club_params],
            answer_query=answer_query, answers=answers,
        ))
    return out


def _apply_gates(generated: list[Generated], cur, quality_gates: dict) -> None:
    """Size and mix gates. Fame is the entity's total de+en pageviews; the mix cutoffs are quantiles
    over all entities of the same type that appear in any answer set."""
    ids = {e for g in generated for e in g.answers}
    cur.execute("select e.id, e.type::text as type, coalesce(sum(v.views), 0)::bigint as fame from entities e"
                " left join entity_pageviews v on v.entity_id = e.id where e.id = any(%s) group by e.id",
                (list(ids),))
    rows = cur.fetchall()
    fame = {r["id"]: r["fame"] for r in rows}
    cutoffs = {t: gates.fame_cutoffs([r["fame"] for r in rows if r["type"] == t], quality_gates)
               for t in {r["type"] for r in rows}}
    for g in generated:
        mix = gates.mix_gate([fame[e] for e in g.answers], cutoffs.get(g.template.answer_type, (0, 0)),
                             quality_gates)
        g.gate_results = gates.gate_results(len(g.answers), quality_gates, mix)
        g.status = gates.status(g.gate_results)


def _store(cur, template: Template, competition_id: int, generated: list[Generated], facts_as_of,
           quality_gates: dict) -> Stats:
    stats = Stats(generated=len(generated))
    for g in generated:
        size, mix = g.gate_results["size"], g.gate_results["mix"]
        stats.gate_passed += g.status == "gate_passed"
        stats.size_passed += size["pass"]
        stats.too_few += size["n"] < size["min"]
        stats.too_many += size["n"] > size["max"]
        stats.too_few_obvious += mix["obvious"] < mix["min_obvious"]
        stats.too_few_obscure += mix["obscure"] < mix["min_obscure"]
    for alt in quality_gates["report_min_answers_alternatives"]:
        stats.pass_at_min[alt] = sum(alt <= len(g.answers) <= quality_gates["max_answers"]
                                     and g.gate_results["mix"]["pass"] for g in generated)

    cur.execute("select id, params, status, text, answer_query, club_ids, gate_results, answer_count"
                " from prompts where competition_id = %s and template = %s", (competition_id, template.id))
    existing = {(template.id, params_key(r["params"])): r for r in cur.fetchall()}
    old_answers: dict[int, dict[int, tuple[list[int], list[str]]]] = {pid: {} for pid in
                                                                       (r["id"] for r in existing.values())}
    cur.execute("select prompt_id, entity_id, evidence_fact_ids, flags from prompt_answers"
                " where prompt_id = any(%s)", ([r["id"] for r in existing.values()],))
    for r in cur.fetchall():
        old_answers[r["prompt_id"]][r["entity_id"]] = (r["evidence_fact_ids"], r["flags"])

    produced = set()
    for g in generated:
        produced.add(g.key)
        row = existing.get(g.key)
        if row is None:
            cur.execute(
                "insert into prompts (template, params, text, answer_type, answer_query, competition_id,"
                " club_ids, status, gate_results, answer_count, facts_as_of)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
                (template.id, Jsonb(g.params), g.text, template.answer_type, g.answer_query, competition_id,
                 g.club_ids, g.status, Jsonb(g.gate_results), len(g.answers), facts_as_of))
            _insert_answers(cur, cur.fetchone()["id"], g.answers)
            stats.inserted += 1
            continue

        before = old_answers[row["id"]]
        if row["status"] == "approved":
            stats.approved += 1
            if before.keys() != g.answers.keys():
                stats.approved_diffs.append(_approved_diff(cur, row, before, g))
            continue

        same = (row["text"], row["answer_query"], row["club_ids"], row["status"], row["gate_results"],
                row["answer_count"], before) == (g.text, g.answer_query, g.club_ids, g.status, g.gate_results,
                                                 len(g.answers), g.answers)
        if same:
            stats.unchanged += 1
            continue
        # facts_as_of moves only with a real change, so re-running after an unrelated ingest is a no-op.
        cur.execute(
            "update prompts set text = %s, answer_query = %s, club_ids = %s, status = %s, gate_results = %s,"
            " answer_count = %s, facts_as_of = %s, updated_at = now() where id = %s",
            (g.text, g.answer_query, g.club_ids, g.status, Jsonb(g.gate_results), len(g.answers), facts_as_of,
             row["id"]))
        if before != g.answers:
            cur.execute("delete from prompt_answers where prompt_id = %s", (row["id"],))
            _insert_answers(cur, row["id"], g.answers)
        stats.updated += 1

    gone = [r for k, r in existing.items() if k not in produced]
    stats.approved_gone = sum(r["status"] == "approved" for r in gone)
    to_delete = [r["id"] for r in gone if r["status"] != "approved"]
    if to_delete:
        cur.execute("delete from prompts where id = any(%s)", (to_delete,))
    stats.deleted = len(to_delete)
    return stats


def _insert_answers(cur, prompt_id: int, answers: dict[int, tuple[list[int], list[str]]]) -> None:
    cur.executemany(
        "insert into prompt_answers (prompt_id, entity_id, evidence_fact_ids, flags) values (%s, %s, %s, %s)",
        [(prompt_id, entity_id, evidence, flags) for entity_id, (evidence, flags) in answers.items()])


def _approved_diff(cur, row: dict, before: dict, g: Generated) -> ApprovedDiff:
    added, removed = g.answers.keys() - before.keys(), before.keys() - g.answers.keys()
    cur.execute("select id, canonical_name from entities where id = any(%s)", (list(added | removed),))
    names = {r["id"]: r["canonical_name"] for r in cur.fetchall()}
    return ApprovedDiff(row["id"], row["text"], sorted(names[i] for i in added), sorted(names[i] for i in removed))


def print_summary(stats: dict[str, Stats], dry_run: bool) -> None:
    print("\n" + "=" * 78 + f"\nPROMPT GENERATION{' (dry run, rolled back)' if dry_run else ''}\n" + "=" * 78)
    for template_id, s in stats.items():
        print(f"\n{template_id}")
        print(f"  generated {s.generated}   gate_passed {s.gate_passed}   "
              f"failed: too few {s.too_few}, too many {s.too_many}, "
              f"too few obvious {s.too_few_obvious}, too few obscure {s.too_few_obscure}")
        print(f"  passed size {s.size_passed}, of which mix removed {s.size_passed - s.gate_passed}")
        print("  would pass (size and mix) at min answers  " + "   ".join(f"{m}: {n}" for m, n in s.pass_at_min.items()))
        print(f"  inserted {s.inserted}   updated {s.updated}   unchanged {s.unchanged}   deleted {s.deleted}")
        if s.approved or s.approved_gone:
            print(f"  approved {s.approved} (answer set differs: {len(s.approved_diffs)}, "
                  f"no longer produced: {s.approved_gone})")
        for d in s.approved_diffs:
            print(f"    prompt {d.prompt_id} \"{d.text}\": +{d.added or '-'} -{d.removed or '-'}")
