"""Idempotent upserts into the reference tables, in one transaction.

Rows are keyed on natural keys (wikidata_id, (entity_id, alias_norm), source_ref, (entity, wiki,
month)), so re-running never duplicates. Facts and aliases this run didn't produce are marked
stale; nothing is deleted. The Bundesliga ingest is the only writer of these tables for now, so
"not seen this run" means "gone from Wikidata or out of scope". Step 7 will scope this per
competition.
"""

import json

import psycopg

from .extract import Extraction


def _copy_upsert(cur: psycopg.Cursor, table: str, columns: list[str], rows: list[tuple],
                 conflict: list[str], update: list[str]) -> None:
    tmp = f"tmp_{table}"
    cols = ", ".join(columns)
    cur.execute(f"create temp table {tmp} on commit drop as select {cols} from {table} with no data")
    with cur.copy(f"copy {tmp} ({cols}) from stdin") as copy:
        for row in rows:
            copy.write_row(row)
    sets = ", ".join(f"{c} = excluded.{c}" for c in update)
    cur.execute(f"insert into {table} ({cols}) select {cols} from {tmp} "
                f"on conflict ({', '.join(conflict)}) do update set {sets}")


def load(conn: psycopg.Connection, ex: Extraction, pageviews: list[tuple] | None) -> dict[str, int]:
    """Writes everything; returns how many rows each table now holds."""
    with conn.transaction(), conn.cursor() as cur:
        entity_cols = ["wikidata_id", "type", "canonical_name", "description", "dewiki_title", "enwiki_title"]
        cur.execute("create temp table tmp_entities on commit drop as "
                    f"select {', '.join(entity_cols)} from entities with no data")
        with cur.copy(f"copy tmp_entities ({', '.join(entity_cols)}) from stdin") as copy:
            for e in ex.entities.values():
                copy.write_row((e.wikidata_id, e.type, e.canonical_name, e.description, e.dewiki_title, e.enwiki_title))
        # updated_at moves only when something actually changed.
        cur.execute(f"""
            insert into entities ({', '.join(entity_cols)})
            select {', '.join(entity_cols)} from tmp_entities
            on conflict (wikidata_id) do update set
              type = excluded.type, canonical_name = excluded.canonical_name,
              description = excluded.description, dewiki_title = excluded.dewiki_title,
              enwiki_title = excluded.enwiki_title, last_seen_at = now(),
              updated_at = case when (entities.type, entities.canonical_name, entities.description,
                                      entities.dewiki_title, entities.enwiki_title)
                                  is distinct from (excluded.type, excluded.canonical_name, excluded.description,
                                                    excluded.dewiki_title, excluded.enwiki_title)
                                then now() else entities.updated_at end
        """)
        cur.execute("select wikidata_id, id from entities")
        ids = dict(cur.fetchall())

        _copy_upsert(
            cur, "entity_aliases", ["entity_id", "alias", "alias_norm", "kind", "lang", "last_seen_at", "stale"],
            [(ids[a.wikidata_id], a.alias, a.alias_norm, a.kind, a.lang, "now", False) for a in ex.aliases.values()],
            conflict=["entity_id", "alias_norm"], update=["alias", "kind", "lang", "last_seen_at", "stale"],
        )

        fact_cols = ["source_ref", "subject_id", "predicate", "object_id", "value_date", "start_year", "end_year",
                     "start_date", "start_precision", "end_date", "end_precision", "date_quality", "apps", "goals",
                     "qualifiers", "source", "last_seen_at", "stale"]
        fact_rows = []
        for f in ex.facts.values():
            obj = ids.get(f.object) if f.object else None
            if f.object and obj is None:
                raise RuntimeError(f"Fact {f.source_ref} points at {f.object}, which has no entity row")
            fact_rows.append((f.source_ref, ids[f.subject], f.predicate, obj, f.value_date, f.start_year, f.end_year,
                              f.start_date, f.start_precision, f.end_date, f.end_precision, f.date_quality,
                              f.apps, f.goals, json.dumps(f.qualifiers, ensure_ascii=False), f.source, "now", False))
        _copy_upsert(cur, "facts", fact_cols, fact_rows, conflict=["source_ref"],
                     update=[c for c in fact_cols if c != "source_ref"])

        if pageviews is not None:
            _copy_upsert(
                cur, "entity_pageviews", ["entity_id", "wiki", "month", "views"],
                [(ids[q], wiki, month, views) for q, wiki, month, views in pageviews],
                conflict=["entity_id", "wiki", "month"], update=["views"],
            )

        # now() is fixed for the whole transaction, so anything upserted above has last_seen_at = now().
        cur.execute("update facts set stale = (last_seen_at < now()) where stale <> (last_seen_at < now())")
        cur.execute("update entity_aliases set stale = (last_seen_at < now()) where stale <> (last_seen_at < now())")

        counts = {}
        for table in ("entities", "entity_aliases", "facts", "entity_pageviews"):
            cur.execute(f"select count(*) from {table}")
            counts[table] = cur.fetchone()[0]
    return counts
