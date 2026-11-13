import { afterAll, describe, expect, it } from "vitest";
import type { Sql } from "@/lib/db/client";
import { asAdmin, asApp, closeAdmin, inRolledBackTx } from "@/lib/test/db";
import { addDive, addUser, makeWorld, type DiveOptions, type World } from "@/lib/test/fixtures";
import { ServiceError } from "./errors";
import { createDiveService, type DiveService } from "./service";

// Service tests against local Supabase (npm run test:db). Fixtures go in as the admin role, then the service runs as
// bk_app, so the real grants and triggers are exercised. Each test is rolled back; the clock is injected.

const T0 = new Date("2030-01-01T12:00:00.000Z");
const OPENS = new Date("2030-01-01T05:00:00.000Z");
const CLOSES = new Date("2030-01-02T05:00:00.000Z");

interface Ctx {
  tx: Sql;
  svc: DiveService;
  world: World;
  userId: number;
  diveId: number;
  promptIds: number[];
  advance(ms: number): void;
  setTime(d: Date): void;
  admin<T>(fn: () => Promise<T>): Promise<T>;
  /** A second player, with a users row. */
  otherUser(): Promise<number>;
  addDive(o: Partial<DiveOptions> & { opensAt: Date; closesAt: Date }): Promise<{ diveId: number; promptIds: number[] }>;
  /** Runs `fn` in a savepoint and returns the database error it raised (null when it succeeded). */
  fails(fn: (s: Sql) => Promise<unknown>): Promise<unknown>;
  rows<T extends object = Record<string, unknown>>(query: TemplateStringsArray, ...args: unknown[]): Promise<T[]>;
}

async function scenario(fn: (c: Ctx) => Promise<void>): Promise<void> {
  await inRolledBackTx(async (tx) => {
    const world = await makeWorld(tx);
    const { userId } = await addUser(tx);
    const { diveId, promptIds } = await addDive(tx, world, { opensAt: OPENS, closesAt: CLOSES });
    await asApp(tx);
    let now = new Date(T0);
    const svc = createDiveService(tx, { now: () => now });
    await fn({
      tx, svc, world, userId, diveId, promptIds,
      advance: (ms) => { now = new Date(now.getTime() + ms); },
      setTime: (d) => { now = d; },
      admin: (f) => asAdmin(tx, f),
      otherUser: () => asAdmin(tx, async () => (await addUser(tx)).userId),
      addDive: (o) => asAdmin(tx, () => addDive(tx, world, o)),
      fails: (f) => (tx as unknown as { savepoint: (g: (s: Sql) => Promise<unknown>) => Promise<unknown> }).savepoint(f).then(() => null, (x: unknown) => x),
      rows: (q, ...args) => tx(q, ...(args as never[])) as never,
    });
  });
}

const rejects = async (p: Promise<unknown>, code: string, status?: number) => {
  const e = await p.then(() => null, (x: unknown) => x);
  expect(e).toBeInstanceOf(ServiceError);
  expect((e as ServiceError).code).toBe(code);
  if (status) expect((e as ServiceError).status).toBe(status);
};

/** Starts the attempt and serves prompt 1. */
async function begin(c: Ctx) {
  const { attempt } = await c.svc.createOrResumeAttempt(c.userId, c.diveId);
  const first = await c.svc.next(c.userId, attempt.id);
  return { attemptId: attempt.id, first };
}

afterAll(closeAdmin);

describe("answering and the server timer", () => {
  it("an answer on time is matched; tier, points and name come from the frozen answer row", async () => {
    await scenario(async (c) => {
      const { attemptId, first } = await begin(c);
      expect(first).toMatchObject({ position: 1, prompt_count: 7, remaining_ms: 25_000 });
      c.advance(4_200);
      const r = await c.svc.answer(c.userId, attemptId, 1, "testmann");
      expect(r).toMatchObject({
        position: 1, verdict: "counted", matched_name: "Anton Testmann (shown)", tier: 1, tier_name: "Bandwagon",
        hipster_pick: false, points: 10,
      });
      const [row] = await c.rows`select verdict, raw_text, input_norm, match_kind, tier, points, matcher_version, response_ms, origin
                                  from answers where attempt_id = ${attemptId}`;
      expect(row).toMatchObject({
        verdict: "matched", raw_text: "testmann", input_norm: "testmann", match_kind: "surname", tier: 1, points: 10,
        matcher_version: "bk-match-1", response_ms: 4_200, origin: "play",
      });
    });
  });

  it("falls back to canonical_name, matches accents, and passes the hipster flag through", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      const a = await c.svc.answer(c.userId, attemptId, 1, "BARBEL mullerprufer");
      expect(a).toMatchObject({ verdict: "counted", matched_name: "Bärbel Müllerprüfer", tier_name: "Real One", points: 50 });
      await c.svc.next(c.userId, attemptId);
      const b = await c.svc.answer(c.userId, attemptId, 2, "Carl Tiefschnitt");
      expect(b).toMatchObject({ verdict: "counted", tier_name: "Deep Cut", hipster_pick: true, points: 15 });
    });
  });

  it("an answer inside the grace still counts; after deadline + grace it is `late` (stored, unmatched, 0 points)", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      c.advance(26_500); // past 25 s, inside the 3 s grace
      expect(await c.svc.answer(c.userId, attemptId, 1, "Testmann")).toMatchObject({ verdict: "counted", points: 10 });
      await c.svc.next(c.userId, attemptId);
      c.advance(28_001);
      const late = await c.svc.answer(c.userId, attemptId, 2, "Testmann");
      expect(late).toMatchObject({ verdict: "too_late", points: 0, matched_name: null, tier_name: null });
      const [row] = await c.rows`select verdict, raw_text, matched_entity_id, tier, points, input_norm
                                  from answers where attempt_id = ${attemptId} and position = 2`;
      expect(row).toMatchObject({ verdict: "late", raw_text: "Testmann", matched_entity_id: null, tier: null, points: 0, input_norm: null });
    });
  });

  it("a refresh mid-prompt keeps remaining_ms; next does not restart the timer or add rows", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      c.advance(7_000);
      const state = await c.svc.getState(c.userId, attemptId);
      expect(state.current).toMatchObject({ position: 1, remaining_ms: 18_000, result: null });
      c.advance(2_000);
      expect(await c.svc.next(c.userId, attemptId)).toMatchObject({ position: 1, remaining_ms: 16_000 });
      expect((await c.svc.getState(c.userId, attemptId)).current?.remaining_ms).toBe(16_000);
      const [n] = await c.rows<{ n: string }>`select count(*) as n from answers where attempt_id = ${attemptId}`;
      expect(Number(n?.n)).toBe(0);
    });
  });

  it("a double submit returns the stored result and writes one row", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      const first = await c.svc.answer(c.userId, attemptId, 1, "Testmann");
      c.advance(1_000);
      const again = await c.svc.answer(c.userId, attemptId, 1, "something else entirely");
      expect(again).toEqual(first);
      const [n] = await c.rows<{ n: string }>`select count(*) as n from answers where attempt_id = ${attemptId}`;
      expect(Number(n?.n)).toBe(1);
      // Even concurrently.
      await c.svc.next(c.userId, attemptId);
      const [x, y] = await Promise.all([
        c.svc.answer(c.userId, attemptId, 2, "Testmann"),
        c.svc.answer(c.userId, attemptId, 2, "Testmann"),
      ]);
      expect(x).toEqual(y);
      const [m] = await c.rows<{ n: string }>`select count(*) as n from answers where attempt_id = ${attemptId} and position = 2`;
      expect(Number(m?.n)).toBe(1);
    });
  });

  it("concurrent next calls advance exactly one prompt", async () => {
    await scenario(async (c) => {
      const { attempt } = await c.svc.createOrResumeAttempt(c.userId, c.diveId);
      const [a, b] = await Promise.all([c.svc.next(c.userId, attempt.id), c.svc.next(c.userId, attempt.id)]);
      expect(a).toMatchObject({ position: 1 });
      expect(b).toMatchObject({ position: 1 });
      await c.svc.answer(c.userId, attempt.id, 1, "Testmann");
      const [p, q] = await Promise.all([c.svc.next(c.userId, attempt.id), c.svc.next(c.userId, attempt.id)]);
      expect(p).toMatchObject({ position: 2 });
      expect(q).toMatchObject({ position: 2 });
      const [row] = await c.rows<{ current_position: number }>`select current_position from attempts where id = ${attempt.id}`;
      expect(row?.current_position).toBe(2);
    });
  });

  it("an expired unanswered prompt gets a timeout row when the next one is requested", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      c.advance(28_001);
      const second = await c.svc.next(c.userId, attemptId);
      expect(second).toMatchObject({ position: 2, remaining_ms: 25_000 });
      const [row] = await c.rows`select verdict, raw_text, answered_at, points, served_at from answers where attempt_id = ${attemptId} and position = 1`;
      expect(row).toMatchObject({ verdict: "timeout", raw_text: null, answered_at: null, points: 0 });
      // Calling next twice does not duplicate it.
      c.advance(1);
      await c.svc.next(c.userId, attemptId);
      const [n] = await c.rows<{ n: string }>`select count(*) as n from answers where attempt_id = ${attemptId}`;
      expect(Number(n?.n)).toBe(1);
    });
  });

  it("rejects the wrong position, over-long text and another position's answer", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      await rejects(c.svc.answer(c.userId, attemptId, 2, "Testmann"), "wrong_position", 409);
      await rejects(c.svc.answer(c.userId, attemptId, 1, "x".repeat(201)), "text_too_long", 400);
      await rejects(c.svc.answer(c.userId, attemptId, "1", "Testmann"), "bad_position", 400);
      await rejects(c.svc.answer(c.userId, attemptId, 1, 5), "bad_text", 400);
      expect(await c.svc.answer(c.userId, attemptId, 1, "x".repeat(200))).toMatchObject({ verdict: "not_counted" });
    });
  });
});

describe("matching leftovers go to the review queue", () => {
  it("an unmatched real entity scores 0 as `checking`; a second occurrence increments the row", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      const r = await c.svc.answer(c.userId, attemptId, 1, "Aussenseiter");
      expect(r).toMatchObject({ verdict: "checking", points: 0, matched_name: null });
      const [q1] = await c.rows`select input_norm, sample_raw, occurrences, candidate_entity_ids, first_answer_id, status from review_queue where prompt_id = ${c.promptIds[0] as number}`;
      expect(q1).toMatchObject({ input_norm: "aussenseiter", sample_raw: "Aussenseiter", occurrences: 1, status: "pending" });
      expect((q1 as { candidate_entity_ids: string[] }).candidate_entity_ids.map(Number)).toEqual([c.world.outsider.id]);

      const other = await c.otherUser();
      const { attempt } = await c.svc.createOrResumeAttempt(other, c.diveId);
      await c.svc.next(other, attempt.id);
      c.advance(1_000);
      await c.svc.answer(other, attempt.id, 1, "AUSSENSEITER");
      const qs = await c.rows<{ occurrences: number; last_seen_at: Date }>`select occurrences, last_seen_at from review_queue where prompt_id = ${c.promptIds[0] as number}`;
      expect(qs).toHaveLength(1);
      expect(qs[0]?.occurrences).toBe(2);
      expect(qs[0]?.last_seen_at).toEqual(new Date(T0.getTime() + 1_000));
    });
  });

  it("a duplicate submit does not bump the review count", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      await c.svc.answer(c.userId, attemptId, 1, "Aussenseiter");
      await c.svc.answer(c.userId, attemptId, 1, "Aussenseiter");
      const [q] = await c.rows<{ occurrences: number }>`select occurrences from review_queue`;
      expect(q?.occurrences).toBe(1);
    });
  });
});

describe("ownership and the one rated attempt", () => {
  it("another user's attempt looks like a missing one on every endpoint", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      const other = await c.otherUser();
      await rejects(c.svc.getState(other, attemptId), "not_found", 404);
      await rejects(c.svc.next(other, attemptId), "not_found", 404);
      await rejects(c.svc.answer(other, attemptId, 1, "Testmann"), "not_found", 404);
      await rejects(c.svc.finish(other, attemptId), "not_found", 404);
      const [n] = await c.rows<{ n: string }>`select count(*) as n from answers`;
      expect(Number(n?.n)).toBe(0);
    });
  });

  it("a second rated attempt for the same dive is impossible, in the service and in the database", async () => {
    await scenario(async (c) => {
      const first = await c.svc.createOrResumeAttempt(c.userId, c.diveId);
      const again = await c.svc.createOrResumeAttempt(c.userId, c.diveId);
      expect(first.created).toBe(true);
      expect(again).toMatchObject({ created: false, attempt: { id: first.attempt.id } });
      const [x, y] = await Promise.all([
        c.svc.createOrResumeAttempt(c.userId, c.diveId), c.svc.createOrResumeAttempt(c.userId, c.diveId),
      ]);
      expect(x.attempt.id).toBe(first.attempt.id);
      expect(y.attempt.id).toBe(first.attempt.id);
      const e = await c.fails((s) => s`insert into attempts (user_id, dive_id, rated) values (${c.userId}, ${c.diveId}, true)`);
      expect((e as { code?: string }).code).toBe("23505");
    });
  });

  it("a closed dive cannot be started; an unknown dive is 404", async () => {
    await scenario(async (c) => {
      const closed = await c.addDive({ date: "2029-12-31", opensAt: new Date("2029-12-31T05:00:00Z"), closesAt: new Date("2030-01-01T05:00:00Z") });
      await rejects(c.svc.createOrResumeAttempt(c.userId, closed.diveId), "dive_closed", 409);
      const future = await c.addDive({ date: "2030-01-05", opensAt: new Date("2030-01-05T05:00:00Z"), closesAt: new Date("2030-01-06T05:00:00Z") });
      await rejects(c.svc.createOrResumeAttempt(c.userId, future.diveId), "dive_closed", 409);
      await rejects(c.svc.createOrResumeAttempt(c.userId, 999_999_999), "not_found", 404);
    });
  });

  it("bk_app cannot change or delete answers (append-only, enforced by the database)", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      await c.svc.answer(c.userId, attemptId, 1, "Testmann");
      for (const run of [
        (s: Sql) => s`update answers set points = 100 where attempt_id = ${attemptId}`,
        (s: Sql) => s`delete from answers where attempt_id = ${attemptId}`,
      ]) {
        const e = await c.fails(run);
        expect(e).not.toBeNull();
      }
      const [row] = await c.rows<{ points: number }>`select points from answers where attempt_id = ${attemptId}`;
      expect(row?.points).toBe(10);
    });
  });
});

describe("choosing the current dive", () => {
  it("picks the open dive with the latest opens_at (a test dive beats today's real dive)", async () => {
    await scenario(async (c) => {
      const test = await c.addDive({
        date: "1900-01-01", isTest: true, opensAt: new Date("2030-01-01T11:00:00Z"), closesAt: new Date("2030-01-01T13:00:00Z"),
      });
      const view = await c.svc.getCurrentDive(c.world.qid, c.userId);
      expect(view.state).toBe("available");
      expect(view.dive?.id).toBe(test.diveId);
      expect(view.dive?.open).toBe(true);
      // Once the test dive has closed, the real one is current again.
      c.setTime(new Date("2030-01-01T13:00:01Z"));
      expect((await c.svc.getCurrentDive(c.world.qid, c.userId)).dive?.id).toBe(c.diveId);
    });
  });

  it("resumes the user's in-progress attempt inside the late-finish window, even after close", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      await c.addDive({ date: "2030-01-02", opensAt: CLOSES, closesAt: new Date("2030-01-03T05:00:00Z") });
      c.setTime(new Date(CLOSES.getTime() + 10 * 60_000)); // 10 min after close; the next dive is open
      const view = await c.svc.getCurrentDive(c.world.qid, c.userId);
      expect(view.dive?.id).toBe(c.diveId);
      expect(view.dive?.open).toBe(false);
      expect(view.attempt).toMatchObject({ id: attemptId, status: "in_progress" });
      // A fresh player gets the new dive.
      const other = await c.otherUser();
      expect((await c.svc.getCurrentDive(c.world.qid, other)).dive?.id).not.toBe(c.diveId);
      // The late attempt can still be played: prompt 1 expired long ago (timeout row), so prompt 2 is served.
      expect(await c.svc.next(c.userId, attemptId)).toMatchObject({ position: 2, remaining_ms: 25_000 });
    });
  });

  it("reports no_dive, and closed once dives have ended", async () => {
    await scenario(async (c) => {
      c.setTime(new Date("2029-06-01T00:00:00Z"));
      expect((await c.svc.getCurrentDive(c.world.qid, null)).state).toBe("no_dive");
      c.setTime(new Date("2030-02-01T00:00:00Z"));
      expect((await c.svc.getCurrentDive(c.world.qid, null)).state).toBe("closed");
      expect((await c.svc.getCurrentDive("Q1", null)).state).toBe("no_dive");
    });
  });
});

describe("abandoned attempts", () => {
  it("are finalised on the user's next request after the late window, with the score cached and no extra rows", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      await c.svc.answer(c.userId, attemptId, 1, "Carl Tiefschnitt"); // 15 points
      await c.svc.next(c.userId, attemptId); // position 2 served, never answered
      c.setTime(new Date(CLOSES.getTime() + 15 * 60_000 + 1));
      const view = await c.svc.getCurrentDive(c.world.qid, c.userId);
      expect(view.attempt).toBeNull(); // no longer resumable
      const [a] = await c.rows`select status, score, finished_at from attempts where id = ${attemptId}`;
      expect(a).toMatchObject({ status: "abandoned", score: 15, finished_at: null });
      const [n] = await c.rows<{ n: string }>`select count(*) as n from answers where attempt_id = ${attemptId}`;
      expect(Number(n?.n)).toBe(1);
      await rejects(c.svc.next(c.userId, attemptId), "attempt_over", 409);
      const state = await c.svc.getState(c.userId, attemptId);
      expect(state.summary).toMatchObject({ status: "abandoned", total: 15, max: 700 });
    });
  });
});

describe("finishing", () => {
  async function playAll(c: Ctx, attemptId: number, answers: string[]) {
    for (let i = 0; i < 7; i++) {
      if (i > 0) await c.svc.next(c.userId, attemptId);
      await c.svc.answer(c.userId, attemptId, i + 1, answers[i] ?? "");
    }
  }

  it("refuses while a position is unresolved, then is idempotent and returns the summary", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      await rejects(c.svc.finish(c.userId, attemptId), "unresolved", 409);
      await c.svc.answer(c.userId, attemptId, 1, "Testmann");
      await rejects(c.svc.finish(c.userId, attemptId), "unresolved", 409);
      for (let i = 2; i <= 7; i++) {
        await c.svc.next(c.userId, attemptId);
        await c.svc.answer(c.userId, attemptId, i, i === 7 ? "" : i === 3 ? "Carl Tiefschnitt" : "Testmann");
      }
      expect(await c.svc.next(c.userId, attemptId)).toEqual({ done: true });
      const s1 = await c.svc.finish(c.userId, attemptId);
      c.advance(60_000);
      const s2 = await c.svc.finish(c.userId, attemptId);
      expect(s2).toEqual(s1);
      expect(s1).toMatchObject({ status: "finished", total: 10 * 5 + 15, max: 700 });
      expect(s1.results).toHaveLength(7);
      expect(s1.results[2]).toMatchObject({ position: 3, answer: "Carl Tiefschnitt", verdict: "counted", hipster_pick: true, points: 15 });
      expect(s1.results[6]).toMatchObject({ verdict: "no_answer", points: 0 });
      const [a] = await c.rows`select status, score, finished_at from attempts where id = ${attemptId}`;
      expect(a).toMatchObject({ status: "finished", score: 65 });
      expect((a as { finished_at: Date }).finished_at).toEqual(T0);
      await rejects(c.svc.next(c.userId, attemptId), "attempt_over", 409);
    });
  });

  it("a state read finishes an attempt whose every position is resolved", async () => {
    await scenario(async (c) => {
      const { attemptId } = await begin(c);
      await playAll(c, attemptId, ["Testmann", "Testmann", "Testmann", "Testmann", "Testmann", "Testmann", "Testmann"]);
      const state = await c.svc.getState(c.userId, attemptId);
      expect(state.status).toBe("finished");
      expect(state.summary?.total).toBe(70);
    });
  });
});

describe("what the browser receives", () => {
  it("never contains answer-set data, shares, candidates or other players' answers", async () => {
    await scenario(async (c) => {
      const out: unknown[] = [];
      const other = await c.otherUser();
      const o = await c.svc.createOrResumeAttempt(other, c.diveId);
      await c.svc.next(other, o.attempt.id);
      await c.svc.answer(other, o.attempt.id, 1, "Carl Tiefschnitt");

      out.push(await c.svc.getCurrentDive(c.world.qid, c.userId));
      const { attempt } = await c.svc.createOrResumeAttempt(c.userId, c.diveId);
      out.push(attempt, await c.svc.getState(c.userId, attempt.id));
      for (let i = 1; i <= 7; i++) {
        out.push(await c.svc.next(c.userId, attempt.id));
        out.push(await c.svc.getState(c.userId, attempt.id));
        out.push(await c.svc.answer(c.userId, attempt.id, i, i === 1 ? "Testmann" : i === 2 ? "Aussenseiter" : "nobody"));
      }
      out.push(await c.svc.finish(c.userId, attempt.id));
      const text = JSON.stringify(out);

      // Answers the player never gave, and the other player's answer, are nowhere in any response.
      expect(text).not.toContain("Bärbel");
      expect(text).not.toContain("Müllerprüfer");
      expect(text).not.toContain("Aussenseiter\"," ); // the outsider's name is never returned (only what they typed)
      expect(text).not.toContain("Dora");
      expect(text).not.toContain("Tiefschnitt"); // the other player's pick
      for (const key of ["predicted_share", "share", "candidate", "answer_set", "entity_id", "answers\":", "outside"]) {
        expect(text).not.toContain(key);
      }
      expect(text).toContain("Anton Testmann (shown)"); // only the answer they actually matched
    });
  });
});
