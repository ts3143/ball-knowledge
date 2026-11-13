import "server-only";
import type { Sql } from "@/lib/db/client";
import type { AnswerEntity, EntityType, OutsideEntity } from "./types";

export interface LoadedPrompt {
  id: number;
  template: string;
  text: string;
  answer_type: EntityType;
  status: string;
  answers: AnswerEntity[];
}

/** The prompt and its answer set with non-stale aliases. Server-side only: answer sets never reach the browser. */
export async function loadAnswerSet(sql: Sql, promptId: number): Promise<LoadedPrompt | null> {
  const [p] = await sql<{ id: string; template: string; text: string; answer_type: EntityType; status: string }[]>`
    select id, template, text, answer_type, status from prompts where id = ${promptId}`;
  if (!p) return null;
  const rows = await sql<{
    entity_id: string; canonical_name: string; predicted_share: number; tier: number | null; points: number | null;
    hipster_pick: boolean; aliases: { alias: string; alias_norm: string; kind: string }[];
  }[]>`
    select pa.entity_id, e.canonical_name, pa.predicted_share, pa.tier, pa.points, pa.hipster_pick,
           coalesce(json_agg(json_build_object('alias', a.alias, 'alias_norm', a.alias_norm, 'kind', a.kind))
                    filter (where a.id is not null), '[]'::json) as aliases
    from prompt_answers pa
    join entities e on e.id = pa.entity_id
    left join entity_aliases a on a.entity_id = e.id and not a.stale
    where pa.prompt_id = ${promptId}
    group by pa.entity_id, e.canonical_name, pa.predicted_share, pa.tier, pa.points, pa.hipster_pick
    order by pa.entity_id`;
  const answers = rows.map<AnswerEntity>((r) => {
    if (r.tier === null || r.points === null || r.predicted_share === null) {
      throw new Error(`prompt ${promptId} has an answer without tier/points/share (entity ${r.entity_id}): not rarity-scored`);
    }
    return {
      entity_id: Number(r.entity_id), type: p.answer_type, canonical_name: r.canonical_name,
      predicted_share: Number(r.predicted_share), tier: Number(r.tier), points: Number(r.points),
      hipster_pick: r.hipster_pick, aliases: r.aliases,
    };
  });
  return { id: Number(p.id), template: p.template, text: p.text, answer_type: p.answer_type, status: p.status, answers };
}

/** Real entities of this type outside the answer set whose non-stale alias exactly equals the normalised input. */
export async function loadOutside(
  sql: Sql, answerType: EntityType, inputNorm: string, excludeIds: Iterable<number>,
): Promise<OutsideEntity[]> {
  if (inputNorm === "") return [];
  const exclude = [...excludeIds];
  const rows = await sql<{ id: string; canonical_name: string }[]>`
    select distinct e.id, e.canonical_name
    from entity_aliases a join entities e on e.id = a.entity_id
    where a.alias_norm = ${inputNorm} and not a.stale and e.type = ${answerType}
      and e.id <> all(${exclude}::bigint[])
    order by e.id limit 100`;
  return rows.map((r) => ({ entity_id: Number(r.id), canonical_name: r.canonical_name }));
}
