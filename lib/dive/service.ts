import "server-only";
import { loadTunables, matchingConfig, type Tunables } from "@/lib/config";
import type { Sql } from "@/lib/db/client";
import { buildIndex, matchAnswer, normalize, MATCHER_VERSION, type MatchResult } from "@/lib/matching";
import { loadAnswerSet, loadOutside } from "@/lib/matching/load";
import type {
  AnswerResponse, AttemptInfo, AttemptState, CurrentDive, CurrentPrompt, NextResult, PlayerResult, PlayerVerdict, Summary,
  SummaryLine,
} from "./service-types";
import { badRequest, conflict, notFound, ServiceError } from "./errors";
import { scoreAttempt, type AnswerRow, type AnswerVerdict } from "./scoring";
import {
  attemptWindowOpen, diveIsOpen, isLate, remainingMs, responseMs, timerConfig, type TimerConfig,
} from "./timer";

// The dive service: everything between the route handlers and Postgres. It takes a db handle (a postgres.js pool
// or a transaction) and an injectable clock. Every write is either idempotent or a conditional statement, so
// concurrent requests (a double tap, two tabs) cannot double count, restart a timer or skip a prompt.
//
// What leaves this module is only what the player needs: prompt text, position, remaining time, and after an
// answer the verdict, matched name, tier name, hipster flag and points. Never an answer set, another player's
// answer, a share or a candidate list (hard rule 2).

export type * from "./service-types";

export const MAX_RAW_TEXT = 200; // answers.raw_text check

export interface ServiceOptions {
  now?: () => Date;
  tunables?: () => Tunables;
}

// Response shapes live in ./service-types (importable by the browser).

// ---- Internals ---------------------------------------------------------------------------------------------

interface AttemptRow {
  id: number;
  user_id: number;
  dive_id: number;
  rated: boolean;
  status: AttemptInfo["status"];
  current_position: number;
  current_served_at: Date | null;
  score: number | null;
  opens_at: Date;
  closes_at: Date;
}

interface PromptRow {
  position: number;
  prompt_id: number;
  text: string;
}

interface AnswerDbRow {
  id: number;
  position: number;
  verdict: AnswerVerdict;
  raw_text: string | null;
  matched_entity_id: number | null;
  tier: number | null;
  hipster_pick: boolean;
  points: number;
}

const num = (v: unknown) => Number(v);
const numOrNull = (v: unknown) => (v === null || v === undefined ? null : Number(v));

const VERDICT_FOR_PLAYER: Record<AnswerVerdict | "unanswered", PlayerVerdict> = {
  matched: "counted",
  wrong: "not_counted",
  unmatched_entity: "checking",
  late: "too_late",
  blank: "no_answer",
  timeout: "no_answer",
  unanswered: "no_answer",
};

function normaliseRow(r: AnswerDbRow): AnswerDbRow {
  return {
    ...r, id: num(r.id), position: num(r.position), matched_entity_id: numOrNull(r.matched_entity_id),
    tier: numOrNull(r.tier), points: num(r.points),
  };
}

/** A transaction when given a pool, a savepoint when already inside one (the db tests run in a transaction). */
function withTx<T>(db: Sql, fn: (tx: Sql) => Promise<T>): Promise<T> {
  const h = db as unknown as {
    savepoint?: (f: (s: Sql) => Promise<T>) => Promise<T>;
    begin: (f: (s: Sql) => Promise<T>) => Promise<T>;
  };
  return typeof h.savepoint === "function" ? h.savepoint(fn) : h.begin(fn);
}

export function createDiveService(db: Sql, options: ServiceOptions = {}) {
  const clock = options.now ?? (() => new Date());
  const tunables = options.tunables ?? (() => loadTunables());

  const cfgOf = (): TimerConfig => timerConfig(tunables());
  const tierName = (tier: number | null): string | null =>
    tier === null ? null : ((tunables().tiers.names as string[])[tier - 1] ?? null);
  const maxPoints = () => Math.max(...(tunables().tiers.points as number[]).map(Number));

  // ---- loaders ----

  // The loaders take an optional handle so finish() can read inside its own transaction (one connection, and the
  // chunk-4 rating replay sees the same snapshot).
  async function loadAttempt(attemptId: number, userId: number, q: Sql = db): Promise<AttemptRow> {
    const rows = await q<AttemptRow[]>`
      select a.id, a.user_id, a.dive_id, a.rated, a.status, a.current_position, a.current_served_at, a.score,
             d.opens_at, d.closes_at
      from attempts a join dives d on d.id = a.dive_id
      where a.id = ${attemptId} and a.user_id = ${userId}`;
    const r = rows[0];
    // Someone else's attempt looks the same as a missing one.
    if (!r) throw notFound("No such attempt");
    return { ...r, id: num(r.id), user_id: num(r.user_id), dive_id: num(r.dive_id), current_position: num(r.current_position), score: numOrNull(r.score) };
  }

  async function loadPrompts(diveId: number, q: Sql = db): Promise<PromptRow[]> {
    const rows = await q<{ position: number; prompt_id: string; text: string }[]>`
      select dp.position, p.id as prompt_id, p.text
      from dive_prompts dp join prompts p on p.id = dp.prompt_id
      where dp.dive_id = ${diveId} order by dp.position`;
    return rows.map((r) => ({ position: num(r.position), prompt_id: num(r.prompt_id), text: r.text }));
  }

  /** The effective row (latest by id) per position of the attempt. */
  async function loadEffective(attemptId: number, q: Sql = db): Promise<AnswerDbRow[]> {
    const rows = await q<AnswerDbRow[]>`
      select id, position, verdict, raw_text, matched_entity_id, tier, hipster_pick, points
      from effective_answers where attempt_id = ${attemptId} order by position`;
    return rows.map(normaliseRow);
  }

  async function entityNames(ids: number[], q: Sql = db): Promise<Map<number, string>> {
    const out = new Map<number, string>();
    if (ids.length === 0) return out;
    const rows = await q<{ id: string; name: string }[]>`
      select id, coalesce(display_name, canonical_name) as name from entities where id = any(${ids}::bigint[])`;
    for (const r of rows) out.set(num(r.id), r.name);
    return out;
  }

  function toResult(row: AnswerDbRow | null, position: number, names: Map<number, string>): PlayerResult {
    if (!row) {
      return { position, verdict: "no_answer", matched_name: null, tier: null, tier_name: null, hipster_pick: false, points: 0 };
    }
    const matched = row.verdict === "matched";
    return {
      position,
      verdict: VERDICT_FOR_PLAYER[row.verdict],
      matched_name: matched && row.matched_entity_id !== null ? (names.get(row.matched_entity_id) ?? null) : null,
      tier: matched ? row.tier : null,
      tier_name: matched ? tierName(row.tier) : null,
      hipster_pick: matched && row.hipster_pick,
      points: matched ? row.points : 0,
    };
  }

  async function resultsFor(rows: AnswerDbRow[], q: Sql = db): Promise<Map<number, PlayerResult>> {
    const names = await entityNames(rows.flatMap((r) => (r.matched_entity_id === null ? [] : [r.matched_entity_id])), q);
    return new Map(rows.map((r) => [r.position, toResult(r, r.position, names)]));
  }

  const toAnswerRow = (r: AnswerDbRow): AnswerRow => ({
    id: r.id, position: r.position, verdict: r.verdict, raw_text: r.raw_text,
    matched_entity_id: r.matched_entity_id, tier: r.tier, hipster_pick: r.hipster_pick, points: r.points,
  });

  async function buildSummary(
    status: AttemptInfo["status"], attemptId: number, prompts: PromptRow[], rows: AnswerDbRow[], q: Sql = db,
  ): Promise<Summary> {
    const score = scoreAttempt(prompts, rows.map(toAnswerRow), maxPoints());
    const results = await resultsFor(rows, q);
    const byPosition = new Map(rows.map((r) => [r.position, r]));
    return {
      attempt_id: attemptId,
      status: status === "abandoned" ? "abandoned" : "finished",
      total: score.total,
      max: score.max,
      results: prompts.map<SummaryLine>((p) => ({
        ...(results.get(p.position) ?? toResult(null, p.position, new Map())),
        text: p.text,
        answer: byPosition.get(p.position)?.raw_text ?? null,
      })),
    };
  }

  // ---- identity ----

  async function findUser(authUserId: string): Promise<number | null> {
    const [r] = await db<{ id: string }[]>`select id from users where auth_user_id = ${authUserId}`;
    return r ? num(r.id) : null;
  }

  async function ensureUser(authUserId: string): Promise<number> {
    await db`insert into users (auth_user_id) values (${authUserId}) on conflict (auth_user_id) do nothing`;
    const id = await findUser(authUserId);
    if (id === null) throw new ServiceError("user_missing", "Could not create the player", 500);
    return id;
  }

  // ---- abandoned attempts ----

  /** Attempts of this user still in progress after their late-finish window are finalised as abandoned. */
  async function finalizeStale(userId: number, now: Date): Promise<void> {
    const cfg = cfgOf();
    const open = await db<{ id: string; dive_id: string; closes_at: Date }[]>`
      select a.id, a.dive_id, d.closes_at from attempts a join dives d on d.id = a.dive_id
      where a.user_id = ${userId} and a.status = 'in_progress'`;
    for (const a of open) {
      if (attemptWindowOpen(a.closes_at, now, cfg)) continue;
      const prompts = await loadPrompts(num(a.dive_id));
      const rows = await loadEffective(num(a.id));
      const { total } = scoreAttempt(prompts, rows.map(toAnswerRow), maxPoints());
      // No extra answer rows: unserved and unanswered positions simply have no effective row (they score 0).
      await db`update attempts set status = 'abandoned', score = ${total}
               where id = ${num(a.id)} and status = 'in_progress'`;
    }
  }

  // ---- current dive ----

  async function getCurrentDive(competitionQid: string, userId: number | null): Promise<CurrentDive> {
    const now = clock();
    const [comp] = await db<{ id: string }[]>`
      select id from entities where wikidata_id = ${competitionQid} and type = 'competition'`;
    if (!comp) return { state: "no_dive", dive: null, attempt: null };
    const competitionId = num(comp.id);
    if (userId !== null) await finalizeStale(userId, now);

    type DiveDb = { id: string; dive_date: string; opens_at: Date; closes_at: Date; prompt_count: string };

    // 1. An attempt the user may still finish (stale ones were just finalised) wins.
    let dive: DiveDb | undefined;
    let attempt: AttemptInfo | null = null;
    if (userId !== null) {
      const [r] = await db<(DiveDb & { attempt_id: string; current_position: number })[]>`
        select d.id, d.dive_date::text as dive_date, d.opens_at, d.closes_at,
               (select count(*) from dive_prompts dp where dp.dive_id = d.id) as prompt_count,
               a.id as attempt_id, a.current_position
        from attempts a join dives d on d.id = a.dive_id
        where a.user_id = ${userId} and a.status = 'in_progress' and d.competition_id = ${competitionId}
        order by d.opens_at desc limit 1`;
      if (r) {
        dive = r;
        attempt = { id: num(r.attempt_id), status: "in_progress", current_position: num(r.current_position), score: null };
      }
    }
    // 2. Otherwise the open dive with the latest opens_at (a test dive from `open-now` beats today's real dive).
    if (!dive) {
      [dive] = await db<DiveDb[]>`
        select d.id, d.dive_date::text as dive_date, d.opens_at, d.closes_at,
               (select count(*) from dive_prompts dp where dp.dive_id = d.id) as prompt_count
        from dives d
        where d.competition_id = ${competitionId} and d.opens_at <= ${now} and ${now} < d.closes_at
        order by d.opens_at desc limit 1`;
    }
    if (!dive) {
      const [past] = await db<{ n: string }[]>`
        select count(*) as n from dives where competition_id = ${competitionId} and closes_at <= ${now}`;
      return { state: num(past?.n ?? 0) > 0 ? "closed" : "no_dive", dive: null, attempt: null };
    }
    if (!attempt && userId !== null) {
      const [a] = await db<{ id: string; status: AttemptInfo["status"]; current_position: number; score: number | null }[]>`
        select id, status, current_position, score from attempts
        where user_id = ${userId} and dive_id = ${dive.id} and rated`;
      if (a) attempt = { id: num(a.id), status: a.status, current_position: num(a.current_position), score: numOrNull(a.score) };
    }
    const cfg = cfgOf();
    return {
      state: "available",
      attempt,
      dive: {
        id: num(dive.id), dive_date: dive.dive_date, prompt_count: num(dive.prompt_count),
        seconds_per_prompt: cfg.seconds_per_prompt, opens_at: dive.opens_at.toISOString(),
        closes_at: dive.closes_at.toISOString(), open: diveIsOpen(dive.opens_at, dive.closes_at, now),
      },
    };
  }

  // ---- attempts ----

  async function createOrResumeAttempt(userId: number, diveId: number): Promise<{ attempt: AttemptInfo; created: boolean }> {
    const now = clock();
    await finalizeStale(userId, now);
    const [dive] = await db<{ opens_at: Date; closes_at: Date; n: string }[]>`
      select d.opens_at, d.closes_at, (select count(*) from dive_prompts dp where dp.dive_id = d.id) as n
      from dives d where d.id = ${diveId}`;
    if (!dive) throw notFound("No such dive");

    const existing = () => db<{ id: string; status: AttemptInfo["status"]; current_position: number; score: number | null }[]>`
      select id, status, current_position, score from attempts where user_id = ${userId} and dive_id = ${diveId} and rated`;
    const info = (a: Awaited<ReturnType<typeof existing>>[number]): AttemptInfo => ({
      id: num(a.id), status: a.status, current_position: num(a.current_position), score: numOrNull(a.score),
    });

    const [had] = await existing();
    if (had) return { attempt: info(had), created: false };

    // Rated only inside opens_at <= now < closes_at. Closed dives cannot be started in step 5 (the archive is step 6).
    if (!diveIsOpen(dive.opens_at, dive.closes_at, now)) {
      throw conflict("dive_closed", now < dive.opens_at ? "This dive has not opened yet." : "This dive has closed.");
    }
    if (num(dive.n) === 0) throw conflict("dive_empty", "This dive has no prompts.");
    // The partial unique index (user_id, dive_id) where rated makes a second rated attempt impossible; a lost
    // race inserts nothing and falls through to the existing row.
    const [created] = await db<{ id: string }[]>`
      insert into attempts (user_id, dive_id, rated, started_at) values (${userId}, ${diveId}, true, ${now})
      on conflict (user_id, dive_id) where rated do nothing returning id`;
    const [row] = await existing();
    if (!row) throw new ServiceError("attempt_missing", "Could not start the attempt", 500);
    return { attempt: info(row), created: Boolean(created) };
  }

  /** Where an attempt stands, and the summary once it is over. */
  async function getState(userId: number, attemptId: number): Promise<AttemptState> {
    const now = clock();
    await finalizeStale(userId, now);
    const attempt = await loadAttempt(attemptId, userId);
    const prompts = await loadPrompts(attempt.dive_id);
    const rows = await loadEffective(attemptId);
    const count = prompts.length;

    if (attempt.status === "in_progress" && count > 0 && prompts.every((p) => rows.some((r) => r.position === p.position))) {
      // Every position is resolved: finishing is also a read-side effect, as in the design (idempotent).
      const summary = await finish(userId, attemptId);
      return { attempt_id: attemptId, status: summary.status, prompt_count: count, current: null, results: [], summary };
    }
    if (attempt.status !== "in_progress") {
      const summary = await buildSummary(attempt.status, attemptId, prompts, rows);
      return { attempt_id: attemptId, status: attempt.status, prompt_count: count, current: null, results: [], summary };
    }

    const results = await resultsFor(rows);
    const k = attempt.current_position;
    const done: PlayerResult[] = [];
    for (const p of prompts) if (p.position < k) done.push(results.get(p.position) ?? toResult(null, p.position, new Map()));
    let current: CurrentPrompt | null = null;
    if (k > 0 && attempt.current_served_at) {
      const p = prompts.find((x) => x.position === k);
      if (p) {
        current = {
          position: k, prompt_count: count, text: p.text,
          remaining_ms: remainingMs(attempt.current_served_at, now, cfgOf()),
          result: results.get(k) ?? null,
        };
      }
    }
    return { attempt_id: attemptId, status: "in_progress", prompt_count: count, current, results: done, summary: null };
  }

  function promptView(prompts: PromptRow[], position: number, count: number, servedAt: Date, now: Date, cfg: TimerConfig): NextResult {
    const p = prompts.find((x) => x.position === position);
    if (!p) throw new ServiceError("prompt_missing", "Prompt missing", 500);
    return { position, prompt_count: count, text: p.text, remaining_ms: remainingMs(servedAt, now, cfg) };
  }

  /**
   * Serves the next prompt. Prompt k still open (unanswered, inside deadline + grace) is returned again: a refresh
   * never restarts the timer. If k expired unanswered it gets a `timeout` row. Then k+1 is served with a
   * conditional update (where current_position = k), so concurrent calls advance one step at most.
   */
  async function next(userId: number, attemptId: number): Promise<NextResult> {
    const now = clock();
    await finalizeStale(userId, now);
    const attempt = await loadAttempt(attemptId, userId);
    if (attempt.status !== "in_progress") throw conflict("attempt_over", "This attempt is already over.");
    const cfg = cfgOf();
    const prompts = await loadPrompts(attempt.dive_id);
    const count = prompts.length;

    const k = attempt.current_position;
    if (k > 0 && attempt.current_served_at) {
      const [have] = await db<{ id: string }[]>`
        select id from answers where attempt_id = ${attemptId} and position = ${k} and origin = 'play'`;
      if (!have) {
        if (!isLate(attempt.current_served_at, now, cfg)) return promptView(prompts, k, count, attempt.current_served_at, now, cfg);
        const p = prompts.find((x) => x.position === k);
        if (!p) throw new ServiceError("prompt_missing", "Prompt missing", 500);
        await db`
          insert into answers (attempt_id, position, prompt_id, origin, verdict, points, served_at, matcher_version)
          values (${attemptId}, ${k}, ${p.prompt_id}, 'play', 'timeout', 0, ${attempt.current_served_at}, ${MATCHER_VERSION})
          on conflict (attempt_id, position) where origin = 'play' do nothing`;
      }
      if (k >= count) return { done: true };
    }

    const target = k + 1;
    const [served] = await db<{ current_served_at: Date }[]>`
      update attempts set current_position = ${target}, current_served_at = ${now}
      where id = ${attemptId} and current_position = ${k} and status = 'in_progress'
      returning current_served_at`;
    if (served) return promptView(prompts, target, count, served.current_served_at, now, cfg);

    // Lost a race: another call already advanced. Report where the attempt stands now (never advance twice).
    const fresh = await loadAttempt(attemptId, userId);
    if (fresh.current_served_at && fresh.current_position > 0) {
      return promptView(prompts, fresh.current_position, count, fresh.current_served_at, now, cfg);
    }
    throw new ServiceError("race", "Please try again", 409);
  }

  /**
   * Records the answer for the current position. After deadline + grace: a `late` row (text stored, not matched,
   * 0 points). A duplicate submit returns the stored result.
   */
  async function answer(userId: number, attemptId: number, position: unknown, text: unknown): Promise<AnswerResponse> {
    const now = clock();
    if (typeof position !== "number" || !Number.isInteger(position)) throw badRequest("bad_position", "position must be an integer");
    if (typeof text !== "string") throw badRequest("bad_text", "text must be a string");
    if (Array.from(text).length > MAX_RAW_TEXT) {
      throw badRequest("text_too_long", `Answers are at most ${MAX_RAW_TEXT} characters.`);
    }
    const pos = position;
    await finalizeStale(userId, now);
    const attempt = await loadAttempt(attemptId, userId);
    const prompts = await loadPrompts(attempt.dive_id);
    const count = prompts.length;
    const stored = async (): Promise<AnswerResponse | null> => {
      const [row] = await db<AnswerDbRow[]>`
        select id, position, verdict, raw_text, matched_entity_id, tier, hipster_pick, points
        from effective_answers where attempt_id = ${attemptId} and position = ${pos}`;
      if (!row) return null;
      const r = normaliseRow(row);
      const names = await entityNames(r.matched_entity_id === null ? [] : [r.matched_entity_id]);
      return { ...toResult(r, pos, names), is_last: pos === count };
    };

    // A duplicate submit (double tap, a retry after a lost response) gets the stored result, whatever the state.
    const dup = await stored();
    if (dup) return dup;

    if (attempt.status !== "in_progress") throw conflict("attempt_over", "This attempt is already over.");
    if (pos !== attempt.current_position || !attempt.current_served_at) {
      throw conflict("wrong_position", "That is not the prompt being played.");
    }
    const prompt = prompts.find((p) => p.position === pos);
    if (!prompt) throw new ServiceError("prompt_missing", "Prompt missing", 500);
    const servedAt = attempt.current_served_at;
    const cfg = cfgOf();
    const ms = responseMs(servedAt, now);
    const mcfg = matchingConfig(tunables());

    let verdict: AnswerVerdict;
    let m: MatchResult | null = null;
    if (isLate(servedAt, now, cfg)) {
      verdict = "late";
    } else {
      const set = await loadAnswerSet(db, prompt.prompt_id);
      if (!set) throw new ServiceError("prompt_missing", "Prompt missing", 500);
      const index = buildIndex(set.answers, mcfg);
      const norm = normalize(Array.from(text).slice(0, mcfg.max_input_chars).join(""));
      const outside = await loadOutside(db, set.answer_type, norm, index.ids);
      m = matchAnswer(text, index, () => outside, mcfg);
      verdict = m.verdict;
    }

    const [ins] = await db<{ id: string }[]>`
      insert into answers (attempt_id, position, prompt_id, origin, raw_text, input_norm, verdict, matched_entity_id,
                           match_kind, edit_distance, ambiguous, tier, hipster_pick, points, served_at, answered_at,
                           response_ms, matcher_version)
      values (${attemptId}, ${pos}, ${prompt.prompt_id}, 'play', ${text}, ${m ? m.input_norm : null}, ${verdict},
              ${m?.matched_entity_id ?? null}, ${m?.match_kind ?? null}, ${m?.edit_distance ?? null},
              ${m?.ambiguous ?? false}, ${m?.tier ?? null}, ${m?.hipster_pick ?? false}, ${m?.points ?? 0},
              ${servedAt}, ${now}, ${ms}, ${MATCHER_VERSION})
      on conflict (attempt_id, position) where origin = 'play' do nothing
      returning id`;

    if (!ins) {
      // Lost a race with a concurrent submit or with `next` writing a timeout: the stored row is the truth.
      const again = await stored();
      if (again) return again;
      throw new ServiceError("race", "Please try again", 409);
    }

    if (m && m.verdict === "unmatched_entity") {
      const ids = m.outside.map((o) => o.entity_id).slice(0, mcfg.review_candidates_max);
      await db`
        insert into review_queue (prompt_id, input_norm, sample_raw, candidate_entity_ids, first_answer_id, last_seen_at)
        values (${prompt.prompt_id}, ${m.input_norm}, ${text}, ${ids}::bigint[], ${num(ins.id)}, ${now})
        on conflict (prompt_id, input_norm)
        do update set occurrences = review_queue.occurrences + 1, last_seen_at = excluded.last_seen_at`;
    }
    const fresh = await stored();
    if (!fresh) throw new ServiceError("race", "Please try again", 500);
    return fresh;
  }

  /**
   * Idempotent. Requires every position resolved (an answer or a timeout row). Sets score, status and finished_at
   * and returns the summary.
   */
  async function finish(userId: number, attemptId: number): Promise<Summary> {
    const now = clock();
    await finalizeStale(userId, now);
    return withTx(db, async (tx) => {
      const [locked] = await tx<{ id: string }[]>`
        select a.id from attempts a where a.id = ${attemptId} and a.user_id = ${userId} for update`;
      if (!locked) throw notFound("No such attempt");
      const attempt = await loadAttempt(attemptId, userId, tx);
      const prompts = await loadPrompts(attempt.dive_id, tx);
      const rows = await loadEffective(attemptId, tx);
      let status = attempt.status;
      if (status === "in_progress") {
        const missing = prompts.filter((p) => !rows.some((r) => r.position === p.position));
        if (prompts.length === 0 || missing.length > 0) {
          throw conflict("unresolved", "Every prompt must be answered or have timed out before the dive can finish.");
        }
        const { total } = scoreAttempt(prompts, rows.map(toAnswerRow), maxPoints());
        await tx`update attempts set status = 'finished', finished_at = ${now}, score = ${total}
                 where id = ${attemptId} and status = 'in_progress'`;
        // ---- SEAM (step 5, chunk 4): the Glicko-2 replay plugs in here, inside this transaction, after the score
        // ---- is written: take the advisory lock, load the user's counted rated attempts, replay(), and rewrite
        // ---- ratings and rating_history.
        status = "finished";
      }
      return buildSummary(status, attemptId, prompts, rows, tx);
    });
  }

  return { findUser, ensureUser, getCurrentDive, createOrResumeAttempt, getState, next, answer, finish, now: clock };
}

export type DiveService = ReturnType<typeof createDiveService>;
