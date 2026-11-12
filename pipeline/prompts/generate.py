"""Generates prompts and their answer sets from the facts, one transaction per run.

Prompts are keyed on (template, params). Candidates are rewritten only when something changed;
approved prompts are never rewritten or deleted (hard rule 6): the run reports how their answer
set would differ and leaves them alone.
"""

import json
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ingest.console import log
from rarity.features import AnswerSet, current_from_year, load_evidence
from rarity.model import ModelParams
from rarity.stage import RARITY_COLUMNS, exact_floats, pageviews_through, rate_for_storage, stored_params

from . import gates
from .templates import TEMPLATES, Template


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
    rarity: dict | None = None  # prompts.rarity, set by _rate for size-passing prompts
    rarity_answers: dict[int, tuple] = field(default_factory=dict)  # entity_id -> RARITY_COLUMNS values
    weak_cap_cost: bool = False  # passes the tier gate on model tiers, fails it on the capped tiers

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
    size_passed: int = 0  # passes the size gate, so it is rated
    gate_passed: int = 0  # passes every blocking gate: size and tiers
    tier_failed: int = 0  # passes size, fails the tier gate
    weak_cap_cost: int = 0  # passes the tier gate on model tiers, fails it on capped tiers
    too_few: int = 0
    too_many: int = 0
    pass_at_min: dict[int, int] = field(default_factory=dict)  # size only
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    approved: int = 0
    approved_gone: int = 0
    approved_params_differ: int = 0  # approved prompts whose stored rarity params differ from the current ones
    approved_diffs: list[ApprovedDiff] = field(default_factory=list)


def params_key(params: dict) -> str:
    return json.dumps(params, sort_keys=True)


def generate(conn: psycopg.Connection, competition_qid: str, tunables: dict,
             template_ids: list[str] | None = None, dry_run: bool = False) -> dict[str, Stats]:
    """Runs the templates against the database behind `conn`; returns stats per template id."""
    stored = [TEMPLATES[t] for t in (template_ids or TEMPLATES)]
    with conn.transaction(force_rollback=dry_run), conn.cursor(row_factory=dict_row) as cur:
        exact_floats(cur)  # stored floats are compared with the new ones
        cur.execute("select id, canonical_name from entities where wikidata_id = %s and type = 'competition'",
                    (competition_qid,))
        comp = cur.fetchone()
        if comp is None:
            raise SystemExit(f"No competition entity {competition_qid}; run the ingest first.")
        cur.execute("select max(last_seen_at) as t from facts")
        facts_as_of = cur.fetchone()["t"]

        ctx = {"competition": competition_qid,
               "teams_per_season": tunables["ingest"]["coverage"]["teams_per_season"],
               "current_from_year": current_from_year(cur, comp["id"], competition_qid, tunables),
               "excluded_countries": list(tunables["prompts"].get("excluded_countries", []))}
        wording_ctx = {"start_year": tunables["ingest"]["start_year"], "competition": comp["canonical_name"]}
        quality_gates = tunables["quality_gates"]

        # All templates are always evaluated; --template only limits what is stored.
        by_template = {}
        for template in TEMPLATES.values():
            log(f"{template.id}: enumerating params")
            by_template[template.id] = _generate_template(conn, cur, template, ctx, wording_ctx)
            log(f"{template.id}: {len(by_template[template.id])} prompts")
        _apply_gates([g for gs in by_template.values() for g in gs], quality_gates)

        _rate([g for t in stored for g in by_template[t.id]], cur, competition_qid, tunables)
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


def _apply_gates(generated: list[Generated], quality_gates: dict) -> None:
    """The size gate; the rarity stage adds the tier gate for the prompts that pass it."""
    for g in generated:
        g.gate_results = gates.gate_results(len(g.answers), quality_gates)
        g.status = gates.status(g.gate_results)


def _rate(generated: list[Generated], cur, competition_qid: str, tunables: dict) -> None:
    """The rarity model, for size-passing prompts of the templates being stored. Each prompt is rated from its
    own answers only, so --template cannot change a value. Adds the tier gate and recomputes the status."""
    targets = [g for g in generated if g.gate_results["size"]["pass"]]
    if not targets:
        return
    ev = load_evidence(cur, [AnswerSet(g.template.id, g.club_ids, {e: a[0] for e, a in g.answers.items()})
                             for g in targets], competition_qid, tunables)
    mp = ModelParams.from_tunables(tunables)
    params, through = stored_params(mp, ev, tunables), pageviews_through(cur)
    for g in targets:
        answer_set = AnswerSet(g.template.id, g.club_ids, {e: a[0] for e, a in g.answers.items()})
        stored = rate_for_storage(answer_set, {e: a[1] for e, a in g.answers.items()}, ev, mp, params, through)
        g.rarity, g.rarity_answers = stored.prompt, stored.answers
        g.gate_results["tiers"] = stored.gate
        g.weak_cap_cost = stored.model_gate_pass and not stored.gate["pass"]
        g.status = gates.status(g.gate_results)


def _store(cur, template: Template, competition_id: int, generated: list[Generated], facts_as_of,
           quality_gates: dict) -> Stats:
    stats = Stats(generated=len(generated))
    for g in generated:
        size = g.gate_results["size"]
        stats.gate_passed += g.status == "gate_passed"
        stats.size_passed += size["pass"]
        stats.tier_failed += g.rarity is not None and not g.gate_results["tiers"]["pass"]
        stats.weak_cap_cost += g.weak_cap_cost
        stats.too_few += size["n"] < size["min"]
        stats.too_many += size["n"] > size["max"]
    for alt in quality_gates["report_min_answers_alternatives"]:
        sized = [g for g in generated if alt <= len(g.answers) <= quality_gates["max_answers"]]
        stats.pass_at_min[alt] = len(sized)

    cur.execute("select id, params, status, text, answer_query, club_ids, gate_results, answer_count, rarity"
                " from prompts where competition_id = %s and template = %s", (competition_id, template.id))
    existing = {(template.id, params_key(r["params"])): r for r in cur.fetchall()}
    old_answers: dict[int, dict[int, tuple]] = {pid: {} for pid in (r["id"] for r in existing.values())}
    cur.execute(f"select prompt_id, entity_id, evidence_fact_ids, flags, {', '.join(RARITY_COLUMNS)}"
                " from prompt_answers where prompt_id = any(%s)", ([r["id"] for r in existing.values()],))
    for r in cur.fetchall():
        rated = tuple(r[c] for c in RARITY_COLUMNS) if r["score"] is not None else None
        old_answers[r["prompt_id"]][r["entity_id"]] = (r["evidence_fact_ids"], r["flags"], rated)

    produced = set()
    for g in generated:
        produced.add(g.key)
        row = existing.get(g.key)
        answers = {e: (*a, g.rarity_answers.get(e)) for e, a in g.answers.items()}
        if row is None:
            cur.execute(
                "insert into prompts (template, params, text, answer_type, answer_query, competition_id,"
                " club_ids, status, gate_results, answer_count, facts_as_of, rarity)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
                (template.id, Jsonb(g.params), g.text, template.answer_type, g.answer_query, competition_id,
                 g.club_ids, g.status, Jsonb(g.gate_results), len(g.answers), facts_as_of, _jsonb(g.rarity)))
            _insert_answers(cur, cur.fetchone()["id"], answers)
            stats.inserted += 1
            continue

        before = old_answers[row["id"]]
        if row["status"] == "approved":
            stats.approved += 1
            if before.keys() != g.answers.keys():
                stats.approved_diffs.append(_approved_diff(cur, row, before, g))
            if g.rarity is not None and (row["rarity"] or {}).get("params") != g.rarity["params"]:
                stats.approved_params_differ += 1
            continue

        same = (row["text"], row["answer_query"], row["club_ids"], row["status"], row["gate_results"],
                row["answer_count"], row["rarity"], before) == (g.text, g.answer_query, g.club_ids, g.status,
                                                                g.gate_results, len(g.answers), g.rarity, answers)
        if same:
            stats.unchanged += 1
            continue
        # facts_as_of moves only with a real change, so re-running after an unrelated ingest is a no-op.
        cur.execute(
            "update prompts set text = %s, answer_query = %s, club_ids = %s, status = %s, gate_results = %s,"
            " answer_count = %s, facts_as_of = %s, rarity = %s, updated_at = now() where id = %s",
            (g.text, g.answer_query, g.club_ids, g.status, Jsonb(g.gate_results), len(g.answers), facts_as_of,
             _jsonb(g.rarity), row["id"]))
        if before != answers:
            cur.execute("delete from prompt_answers where prompt_id = %s", (row["id"],))
            _insert_answers(cur, row["id"], answers)
        stats.updated += 1

    gone = [r for k, r in existing.items() if k not in produced]
    stats.approved_gone = sum(r["status"] == "approved" for r in gone)
    to_delete = [r["id"] for r in gone if r["status"] != "approved"]
    if to_delete:
        cur.execute("delete from prompts where id = any(%s)", (to_delete,))
    stats.deleted = len(to_delete)
    return stats


def _jsonb(value):
    """A jsonb parameter that is SQL NULL, not JSON null, for None (a JSON null would pass `rarity is not null`)."""
    return None if value is None else Jsonb(value)


def _insert_answers(cur, prompt_id: int, answers: dict[int, tuple]) -> None:
    """answers: entity_id -> (evidence_fact_ids, flags, RARITY_COLUMNS values or None when not rated)."""
    unrated = (None,) * len(RARITY_COLUMNS[:-1]) + (False,)
    cur.executemany(
        f"insert into prompt_answers (prompt_id, entity_id, evidence_fact_ids, flags, {', '.join(RARITY_COLUMNS)})"
        f" values (%s, %s, %s, %s, {', '.join(['%s'] * len(RARITY_COLUMNS))})",
        [(prompt_id, entity_id, evidence, flags, *(rated or unrated))
         for entity_id, (evidence, flags, rated) in answers.items()])


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
              f"failed: too few {s.too_few}, too many {s.too_many}, tier gate {s.tier_failed}")
        print("  would pass size at min answers  " + "   ".join(f"{m}: {n}" for m, n in s.pass_at_min.items()))
        print(f"  size gate passed {s.size_passed}, tier gate blocks {s.tier_failed} of those "
              f"(the weak-answer cap alone costs {s.weak_cap_cost})")
        print(f"  inserted {s.inserted}   updated {s.updated}   unchanged {s.unchanged}   deleted {s.deleted}")
        if s.approved or s.approved_gone:
            print(f"  approved {s.approved} (answer set differs: {len(s.approved_diffs)}, "
                  f"no longer produced: {s.approved_gone}, rarity params differ from the current: "
                  f"{s.approved_params_differ})")
        for d in s.approved_diffs:
            print(f"    prompt {d.prompt_id} \"{d.text}\": +{d.added or '-'} -{d.removed or '-'}")
