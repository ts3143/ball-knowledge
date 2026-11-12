import { describe, expect, it } from "vitest";
import {
  clubVisible, idleRd, isUncertain, newVolatility, rdNow, ratePeriod, replay, seedRating, VolatilityError,
  type Game, type Opponent, type Params, type Prompt, type State,
} from "./index";

const params: Params = {
  initial_rating: 1500, initial_rd: 350, initial_volatility: 0.06, tau: 0.5, volatility_epsilon: 1e-6,
  child_scope_initial_rd: 250, game_weight: 1.0, uncertain_rd_threshold: 110, club_rating_min_prompts: 20,
  prompt_lock_answers: 200, points_scale: 100,
  seed: { intercept: 1500, rd: 350, coefficients: { log_recall_mass: 40 } },
};

const expectedScore = (rating: number, o: Opponent): number => {
  const phi = o.rd / 173.7178;
  const g = 1 / Math.sqrt(1 + (3 * phi * phi) / (Math.PI * Math.PI));
  return 1 / (1 + Math.exp(-g * ((rating - o.rating) / 173.7178)));
};

describe("core", () => {
  it("leaves the rating unchanged when the score equals E, and shrinks RD", () => {
    const opp: Opponent = { rating: 1500, rd: 100, score: 0 };
    opp.score = expectedScore(1500, opp);
    const out = ratePeriod(1500, 200, 0.06, 0, [opp], params);
    expect(out.rating).toBeCloseTo(1500, 9);
    expect(out.rd).toBeLessThan(200);
  });

  it("lowers the rating for a 0.25 score against an equal opponent", () => {
    const out = ratePeriod(1500, 200, 0.06, 0, [{ rating: 1500, rd: 200, score: 0.25 }], params);
    expect(out.rating).toBeLessThan(1500);
  });

  it("rises with the score and with the opponent's rating", () => {
    const at = (rating: number, score: number) =>
      ratePeriod(1500, 200, 0.06, 0, [{ rating, rd: 100, score }], params).rating;
    expect(at(1500, 0.2)).toBeLessThan(at(1500, 0.5));
    expect(at(1500, 0.5)).toBeLessThan(at(1500, 0.8));
    expect(at(1400, 0.5)).toBeLessThan(at(1600, 0.5));
  });

  it("grows phi^2 by volatility^2 per idle period and caps at initial_rd", () => {
    const phi = 50 / 173.7178;
    expect(idleRd(50, 0.06, 3, 350)).toBeCloseTo(Math.sqrt(phi * phi + 3 * 0.06 * 0.06) * 173.7178, 9);
    expect(idleRd(340, 0.06, 500, 350)).toBeCloseTo(350, 9);
    expect(idleRd(50, 0.06, 0, 350)).toBe(50);
    const idleOnly = ratePeriod(1620, 50, 0.06, 364, [], params);
    expect(idleOnly.rating).toBe(1620);
    expect(idleOnly.rd).toBeGreaterThan(190);
    expect(idleOnly.rd).toBeLessThanOrEqual(350);
  });

  it("stays finite at extremes", () => {
    for (const out of [
      ratePeriod(1500, 1, 0.06, 0, [{ rating: 4500, rd: 30, score: 0 }], params),
      ratePeriod(1500, 350, 0.06, 0, [{ rating: -1500, rd: 350, score: 1 }], params),
    ]) {
      expect(Number.isFinite(out.rating) && Number.isFinite(out.rd) && Number.isFinite(out.volatility)).toBe(true);
    }
  });

  it("uses both bracketing branches and converges within 100 iterations on seeded random cases", () => {
    let state = 12345;
    const rnd = () => ((state = (Math.imul(state, 1664525) + 1013904223) >>> 0) / 2 ** 32);
    const seen = new Set<string>();
    for (let i = 0; i < 10_000; i++) {
      const opponents: Opponent[] = Array.from({ length: 1 + Math.floor(rnd() * 10) }, () => ({
        rating: 1000 + rnd() * 1200, rd: 30 + rnd() * 320, score: Math.floor(rnd() * 5) / 4,
      }));
      const out = ratePeriod(1100 + rnd() * 800, 30 + rnd() * 320, 0.02 + rnd() * 0.08, 0, opponents, params);
      expect(out.iterations).toBeLessThanOrEqual(100);
      seen.add(out.bracket);
    }
    expect([...seen].sort()).toEqual(["log", "scan"]);
  });

  it("raises VolatilityError when the iteration cap is hit", () => {
    expect(() => newVolatility(1, 0.06, 0, 1, 0.5, 0)).toThrow(VolatilityError);
  });

  it("game_weight 1 is standard and a larger weight shrinks RD more", () => {
    const opps: Opponent[] = [{ rating: 1500, rd: 350, score: 0.25 }];
    const w1 = ratePeriod(1500, 350, 0.06, 0, opps, params);
    const w3 = ratePeriod(1500, 350, 0.06, 0, opps, { ...params, game_weight: 3 });
    expect(w3.rd).toBeLessThan(w1.rd);
  });
});

describe("seed and display", () => {
  it("sums sorted coefficients plus the offset, and throws on a missing feature", () => {
    expect(seedRating({ log_recall_mass: -2 }, params.seed, 5)).toBe(1500 - 80 + 5);
    expect(() => seedRating({}, params.seed)).toThrow(/log_recall_mass/);
  });

  it("computes RD today, the uncertain marker and club visibility", () => {
    const st: State = { rating: 1500, rd: 100, volatility: 0.06, games: 10, last_period: "2030-01-01" };
    expect(rdNow(st, "2030-01-02", params)).toBe(100);
    expect(rdNow(st, "2030-01-11", params)).toBeGreaterThan(100);
    expect(rdNow(null, "2030-01-02", params)).toBe(350);
    expect(isUncertain(st, "2030-01-02", params)).toBe(false);
    expect(isUncertain({ ...st, rd: 120 }, "2030-01-02", params)).toBe(true);
    expect(clubVisible(st, params)).toBe(false);
    expect(clubVisible({ ...st, games: 20 }, params)).toBe(true);
  });
});

function prompts(n: number, clubs: Record<number, number[]> = {}): Prompt[] {
  return Array.from({ length: n }, (_, i) => ({
    id: i + 1, competition_id: 1, club_ids: clubs[i + 1] ?? [], features: { log_recall_mass: -1 - (i % 5) * 0.5 },
    template_offset: 0, sport: "football",
  }));
}

function dive(date: string, user: number, attempt: number, ids: number[], points: number[],
              extra: Partial<Game> = {}): Game[] {
  return ids.map((id, i) => ({
    user_id: user, date, attempt, prompt_id: id, points: points[i % points.length] as number, rated: true,
    unanswered: false, ...extra,
  }));
}

describe("engine", () => {
  const ids = [1, 2, 3, 4, 5, 6, 7];

  it("gives the same output when users are shuffled within a date", () => {
    const games = [
      ...dive("2030-01-01", 1, 1, ids, [10, 50, 0, 25, 75, 100, 15]),
      ...dive("2030-01-01", 2, 1, ids, [100, 25, 50, 0, 10, 15, 75]),
      ...dive("2030-01-01", 3, 1, ids, [0, 0, 10, 25, 0, 50, 10]),
      ...dive("2030-01-02", 1, 1, ids, [25, 25, 25, 25, 25, 25, 25]),
      ...dive("2030-01-02", 3, 1, ids, [75, 10, 0, 50, 25, 100, 15]),
    ];
    const reordered = [...games].reverse();
    const ps = prompts(7, { 1: [11], 3: [11], 5: [12] });
    expect(replay(ps, reordered, params)).toEqual(replay(ps, games, params));
  });

  it("starts a child scope from the parent's state at the start of the date, with max(child RD, parent RD)", () => {
    const ps = prompts(14, Object.fromEntries(ids.map((i) => [i + 7, [11]])));
    const games = [...dive("2030-01-01", 1, 1, ids, [25]), ...dive("2030-01-02", 1, 1, ids.map((i) => i + 7), [50])];
    const r = replay(ps, games, params);
    const row = r.history.find((h) => h.scope_type === "club" && h.date === "2030-01-02")!;
    const comp = r.history.find((h) => h.scope_type === "competition" && h.date === "2030-01-02")!;
    expect(row.rating_before).toBe(comp.rating_before);
    expect(row.rd_before).toBe(Math.max(250, comp.rd_before));
    expect(row.volatility_before).toBe(0.06);
  });

  it("freezes prompts within a date and settles them once", () => {
    const games = [...dive("2030-01-01", 1, 1, ids, [50]), ...dive("2030-01-01", 2, 1, ids, [50])];
    const r = replay(prompts(7), games, params);
    for (const ps of r.prompts.values()) {
      expect(ps.games).toBe(2);
      expect(ps.last_period).toBe("2030-01-01");
    }
    const hist = r.history.filter((h) => h.scope_type === "competition");
    expect(hist).toHaveLength(2);
    expect(hist[0]!.rating_after).toBe(hist[1]!.rating_after); // same results against the same frozen prompts
  });

  it("locks a prompt at the threshold and then leaves it alone", () => {
    const p = { ...params, prompt_lock_answers: 2 };
    const games = [...dive("2030-01-01", 1, 1, ids, [50]), ...dive("2030-01-01", 2, 1, ids, [50]),
      ...dive("2030-01-02", 1, 1, ids, [0])];
    const r = replay(prompts(7), games, p);
    const one = replay(prompts(7), games.filter((g) => g.date === "2030-01-01"), p);
    for (const [pid, ps] of r.prompts) {
      expect(ps.locked).toBe(true);
      expect(ps.games).toBe(2);
      expect(ps).toEqual(one.prompts.get(pid));
    }
  });

  it("excludes archive plays and counts an abandoned attempt as 7 games", () => {
    const games = [
      ...dive("2030-01-01", 1, 1, ids, [50, 50, 50, 0, 0, 0, 0]).map((g, i) => (i >= 3 ? { ...g, unanswered: true } : g)),
      ...dive("2030-01-02", 1, 1, ids, [100], { rated: false }),
    ];
    const r = replay(prompts(7), games, params);
    expect(r.history.every((h) => h.date === "2030-01-01")).toBe(true);
    const comp = r.scopes.find((s) => s.scope[0] === "competition")!;
    expect(comp.state.games).toBe(7);
    expect(comp.state.last_period).toBe("2030-01-01");
  });

  it("makes two attempts on one date equal one batch, and grows RD over an idle gap", () => {
    const ps = prompts(14);
    const a = replay(ps, [...dive("2030-01-01", 1, 1, ids, [25]), ...dive("2030-01-01", 1, 2, ids.map((i) => i + 7), [50])],
      params);
    const last = a.history.filter((h) => h.scope_type === "sport").at(-1)!;
    expect(last.games).toBe(14);
    const gap = replay(prompts(14), [...dive("2030-01-01", 1, 1, ids, [25]), ...dive("2030-01-11", 1, 1, ids.map((i) => i + 7), [25])],
      params);
    const row = gap.history.filter((h) => h.scope_type === "sport").at(-1)!;
    const prev = gap.history.filter((h) => h.scope_type === "sport")[0]!;
    expect(row.rd_before).toBeCloseTo(idleRd(prev.rd_after, prev.volatility_after, 9, 350), 9);
  });

  it("rejects games for unknown prompts and duplicate prompt ids", () => {
    expect(() => replay(prompts(1), dive("2030-01-01", 1, 1, [9], [10]), params)).toThrow(/unknown prompt/);
    expect(() => replay([...prompts(1), ...prompts(1)], [], params)).toThrow(/duplicate/);
  });
});
