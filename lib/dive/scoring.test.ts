import { describe, expect, it } from "vitest";
import { loadTunables } from "@/lib/config";
import { effectiveAnswers, scoreAttempt, type AnswerRow } from "./scoring";

const prompts = Array.from({ length: 7 }, (_, i) => ({ position: i + 1, prompt_id: 100 + i }));
const max = Math.max(...loadTunables().tiers.points);

function row(id: number, position: number, over: Partial<AnswerRow> = {}): AnswerRow {
  return { id, position, verdict: "matched", raw_text: "x", matched_entity_id: 1, tier: 2, hipster_pick: false, points: 25, ...over };
}

describe("scoring", () => {
  it("total is out of prompt_count * max(points) = 700", () => {
    const s = scoreAttempt(prompts, [], max);
    expect(s).toMatchObject({ total: 0, max: 700 });
    expect(s.results.every((r) => r.verdict === "unanswered" && !r.answered)).toBe(true);
  });

  it("sums points of the effective rows", () => {
    const rows = [row(1, 1, { points: 100, tier: 5 }), row(2, 2), row(3, 3, { verdict: "wrong", points: 0, tier: null })];
    const s = scoreAttempt(prompts, rows, max);
    expect(s.total).toBe(125);
    expect(s.results[2]).toMatchObject({ verdict: "wrong", points: 0 });
    expect(s.results[3]).toMatchObject({ verdict: "unanswered", points: 0 });
  });

  it("the latest row by id wins (a review correction)", () => {
    const rows = [
      row(9, 1, { verdict: "unmatched_entity", points: 0, tier: null, matched_entity_id: null }),
      row(12, 1, { points: 50, tier: 3 }),
      row(10, 1, { verdict: "wrong", points: 0 }),
    ];
    expect(effectiveAnswers(rows).get(1)?.id).toBe(12);
    expect(scoreAttempt(prompts, rows, max).results[0]).toMatchObject({ points: 50, tier: 3 });
  });

  it("a late or timeout row scores 0 even if points were stored", () => {
    const rows = [row(1, 1, { verdict: "late", points: 75 }), row(2, 2, { verdict: "timeout", points: 75 })];
    expect(scoreAttempt(prompts, rows, max).total).toBe(0);
  });

  it("keeps the hipster flag and orders results by position", () => {
    const s = scoreAttempt([...prompts].reverse(), [row(1, 4, { hipster_pick: true, points: 15 })], max);
    expect(s.results.map((r) => r.position)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(s.results[3]).toMatchObject({ hipster_pick: true, points: 15 });
  });

  it("a full dive of top answers is 700", () => {
    const rows = prompts.map((p, i) => row(i + 1, p.position, { points: 100, tier: 5 }));
    expect(scoreAttempt(prompts, rows, max)).toMatchObject({ total: 700, max: 700 });
  });
});
