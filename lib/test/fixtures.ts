// Synthetic placeholder rows for the db tests. Nothing here is a sports fact: names are made up ("Testmann",
// "FC Testhausen") and every number is arbitrary.
import type { Sql } from "@/lib/db/client";
import { normalize } from "@/lib/matching";

let counter = 0;
const nextQid = () => `Q9991${String(Date.now() % 100000).padStart(5, "0")}${String(++counter).padStart(3, "0")}`;

export interface Person {
  id: number;
  name: string;
}

export interface World {
  competitionId: number;
  qid: string;
  /** Answer-set people, in order: Bandwagon (10), Real One (50), Deep Cut hipster pick (15). */
  people: { common: Person; accented: Person; hipster: Person };
  /** A real person in the database who is in no answer set (matches an alias, so it is `unmatched_entity`). */
  outsider: Person;
}

async function entity(tx: Sql, type: string, name: string, display: string | null = null): Promise<number> {
  const [r] = await tx<{ id: string }[]>`
    insert into entities (type, wikidata_id, canonical_name, display_name)
    values (${type}::entity_type, ${nextQid()}, ${name}, ${display}) returning id`;
  return Number((r as { id: string }).id);
}

async function alias(tx: Sql, entityId: number, text: string, kind: string): Promise<void> {
  await tx`insert into entity_aliases (entity_id, alias, alias_norm, kind) values (${entityId}, ${text}, ${normalize(text)}, ${kind})`;
}

async function person(tx: Sql, name: string, family: string, display: string | null = null): Promise<Person> {
  const id = await entity(tx, "person", name, display);
  await alias(tx, id, name, "label");
  await alias(tx, id, family, "family_name");
  return { id, name: display ?? name };
}

export async function makeWorld(tx: Sql): Promise<World> {
  const competitionId = await entity(tx, "competition", "Test Liga");
  const [{ wikidata_id: qid } = { wikidata_id: "" }] = await tx<{ wikidata_id: string }[]>`
    select wikidata_id from entities where id = ${competitionId}`;
  const common = await person(tx, "Anton Testmann", "Testmann", "Anton Testmann (shown)");
  const accented = await person(tx, "Bärbel Müllerprüfer", "Müllerprüfer");
  const hipster = await person(tx, "Carl Tiefschnitt", "Tiefschnitt");
  const outsider = await person(tx, "Dora Aussenseiter", "Aussenseiter");
  return { competitionId, qid, people: { common, accented, hipster }, outsider };
}

export async function addUser(tx: Sql): Promise<{ userId: number; authId: string }> {
  const [a] = await tx<{ id: string }[]>`insert into auth.users (id) values (gen_random_uuid()) returning id`;
  const authId = (a as { id: string }).id;
  const [u] = await tx<{ id: string }[]>`insert into users (auth_user_id) values (${authId}) returning id`;
  return { userId: Number((u as { id: string }).id), authId };
}

export interface DiveOptions {
  date?: string;
  opensAt: Date;
  closesAt: Date;
  isTest?: boolean;
  prompts?: number;
}

/** A dive of approved prompts, each with the world's three answers. Returns the dive id and its prompt ids. */
export async function addDive(tx: Sql, w: World, o: DiveOptions): Promise<{ diveId: number; promptIds: number[] }> {
  const count = o.prompts ?? 7;
  const promptIds: number[] = [];
  const shares: [Person, number, number, number, boolean][] = [
    [w.people.common, 0.7, 1, 10, false],
    [w.people.accented, 0.2, 3, 50, false],
    [w.people.hipster, 0.1, 4, 15, true],
  ];
  for (let i = 1; i <= count; i++) {
    const [p] = await tx<{ id: string }[]>`
      insert into prompts (template, params, text, answer_type, answer_query, competition_id, status, gate_results,
                           answer_count, facts_as_of, rarity)
      values ('test_template', ${tx.json({ n: ++counter, w: w.competitionId })}, ${`Name a placeholder, number ${i}`},
              'person', 'select 1', ${w.competitionId}, 'approved', ${tx.json({})}, 3, now(),
              ${tx.json({ model_version: "test", log_recall_mass: -3 })})
      returning id`;
    const pid = Number((p as { id: string }).id);
    promptIds.push(pid);
    for (const [who, share, tier, points, hip] of shares) {
      await tx`
        insert into prompt_answers (prompt_id, entity_id, evidence_fact_ids, flags, views_de, views_en, tie_amount,
                                    seasons_since, score, predicted_share, model_tier, tier, points, hipster_pick)
        values (${pid}, ${who.id}, '{}', '{}', 10, 10, 1, 0, ${-share}, ${share}, ${tier}, ${tier}, ${points}, ${hip})`;
    }
  }
  const date = o.date ?? "2030-01-01";
  const [d] = await tx<{ id: string }[]>`
    insert into dives (competition_id, sport, dive_date, opens_at, closes_at, is_test)
    values (${w.competitionId}, 'football', ${date}::date, ${o.opensAt}, ${o.closesAt}, ${o.isTest ?? false})
    returning id`;
  const diveId = Number((d as { id: string }).id);
  for (let i = 0; i < promptIds.length; i++) {
    await tx`insert into dive_prompts (dive_id, position, prompt_id) values (${diveId}, ${i + 1}, ${promptIds[i] as number})`;
  }
  return { diveId, promptIds };
}
