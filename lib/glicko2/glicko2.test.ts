import { describe, expect, it } from "vitest";
import {
  addDays, clubVisible, DegeneratePeriodError, idleRd, isUncertain, newVolatility, promptFeatures, rdNow, ratePeriod,
  replay, seedRating, VolatilityError,
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

  it("raises DegeneratePeriodError when expected results saturate", () => {
    expect(() => ratePeriod(1500, 50, 0.06, 0, [{ rating: -6000, rd: 30, score: 1 }], params)).toThrow(DegeneratePeriodError);
    expect(() => ratePeriod(1500, 50, 0.06, 0, [{ rating: 200000, rd: 30, score: 0 }], params)).toThrow(DegeneratePeriodError);
    const out = ratePeriod(1500, 50, 0.06, 0, [{ rating: -6000, rd: 30, score: 1 }, { rating: 1500, rd: 30, score: 0.5 }], params);
    expect(Number.isFinite(out.rating)).toBe(true);
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

  it("takes ln(n_eff) as a feature and rejects bad prompts rows", () => {
    const f = promptFeatures({ log_recall_mass: 11.25 }, { tiers: { n_eff: 7.5, top_share: 0.3 } });
    expect(f).toEqual({ log_recall_mass: 11.25, ln_n_eff: Math.log(7.5) });
    const bad: [Record<string, unknown> | null, Record<string, unknown>][] = [
      [{ log_recall_mass: 1 }, { tiers: { n_eff: 0 } }], [{ log_recall_mass: 1 }, { tiers: { n_eff: -1 } }],
      [{ log_recall_mass: 1 }, { tiers: {} }], [{ log_recall_mass: 1 }, {}], [{}, { tiers: { n_eff: 3 } }],
      [{ log_recall_mass: null }, { tiers: { n_eff: 3 } }], [{ log_recall_mass: 1 }, { tiers: { n_eff: "7" } }],
      [null, { tiers: { n_eff: 3 } }],
    ];
    for (const [r, g] of bad) expect(() => promptFeatures(r, g)).toThrow();
  });

  it("rejects null, string and non-finite seed features", () => {
    for (const bad of [null, "11", NaN, Infinity]) {
      expect(() => seedRating({ log_recall_mass: bad as unknown as number }, params.seed)).toThrow(/finite/);
    }
    expect(() => seedRating({ log_recall_mass: 1 }, params.seed, NaN)).toThrow(/finite/);
  });

  it("rejects malformed dates in the display helpers", () => {
    expect(() => addDays("2030-1-1", 1)).toThrow(/YYYY-MM-DD/);
    expect(() => rdNow({ rating: 1500, rd: 100, volatility: 0.06, games: 1, last_period: "2030-01-01T00:00:00Z" }, "2030-01-05", params))
      .toThrow(/YYYY-MM-DD/);
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

  it("makes two attempts on one date equal one batch", () => {
    const ps = prompts(14);
    const first = dive("2030-01-01", 1, 1, ids, [25, 50, 0, 75, 10, 100, 15]);
    const second = dive("2030-01-01", 1, 2, ids.map((i) => i + 7), [50, 10, 25, 0, 100, 75, 50]);
    const two = replay(ps, [...first, ...second], params);
    const batch = replay(ps, [...first, ...second].map((g) => ({ ...g, attempt: 1 })), params);
    expect(two.scopes).toEqual(batch.scopes);
    expect(two.prompts).toEqual(batch.prompts);
    const lastTwo = two.history.filter((h) => h.scope_type === "sport").at(-1)!;
    const only = batch.history.filter((h) => h.scope_type === "sport");
    expect(only).toHaveLength(1);
    expect(lastTwo.games).toBe(14);
    expect([lastTwo.rating_after, lastTwo.rd_after, lastTwo.volatility_after])
      .toEqual([only[0]!.rating_after, only[0]!.rd_after, only[0]!.volatility_after]);
  });

  it("grows RD over an idle gap", () => {
    const gap = replay(prompts(14), [...dive("2030-01-01", 1, 1, ids, [25]), ...dive("2030-01-11", 1, 1, ids.map((i) => i + 7), [25])],
      params);
    const rows = gap.history.filter((h) => h.scope_type === "sport");
    expect(rows[1]!.rd_before).toBeCloseTo(idleRd(rows[0]!.rd_after, rows[0]!.volatility_after, 9, 350), 9);
  });

  it("chooses a club's parent per user, so other users never change it", () => {
    const clubs = { 1: [31], 8: [31] };
    const ps: Prompt[] = [...prompts(7).map((p) => ({ ...p, competition_id: 2, club_ids: clubs[p.id as 1] ?? [] })),
      ...prompts(14).slice(7).map((p) => ({ ...p, club_ids: clubs[p.id as 8] ?? [] })),
      ...prompts(21).slice(14), ...prompts(28).slice(21).map((p) => ({ ...p, competition_id: 2 }))];
    const user1 = [...dive("2030-01-01", 1, 1, [15, 16, 17, 18, 19, 20, 21], [100]),
      ...dive("2030-01-01", 1, 2, [22, 23, 24, 25, 26, 27, 28], [0]), ...dive("2030-01-02", 1, 1, ids, [50])];
    const user2 = dive("2030-01-02", 2, 1, [8, 9, 10, 11, 12, 13, 14], [25]);
    const alone = replay(ps, user1, params);
    const both = replay(ps, [...user1, ...user2], params);
    const club = (r: typeof alone, u: number) => r.scopes.find((s) => s.user_id === u && s.scope[0] === "club")!.state;
    expect(club(both, 1)).toEqual(club(alone, 1));
    expect(both.history.filter((h) => h.user_id === 1)).toEqual(alone.history.filter((h) => h.user_id === 1));
    const rowOf = (u: number, type: string, id: number | string) =>
      both.history.find((h) => h.user_id === u && h.date === "2030-01-02" && h.scope_type === type && h.scope_id === id)!;
    expect(rowOf(1, "club", 31).rating_before).toBe(rowOf(1, "competition", 2).rating_before);
    expect(rowOf(2, "club", 31).rating_before).toBe(rowOf(2, "competition", 1).rating_before);
  });

  it("takes the lowest competition id for a club's parent on a date tie", () => {
    const ps: Prompt[] = [
      ...prompts(7).map((p) => ({ ...p, competition_id: 2, club_ids: p.id === 1 ? [31] : [] })),
      ...prompts(14).slice(7).map((p) => ({ ...p, club_ids: p.id === 8 ? [31] : [] })),
      ...prompts(21).slice(14),
      ...prompts(28).slice(21).map((p) => ({ ...p, competition_id: 2 })),
    ];
    const games = [
      ...dive("2030-01-01", 1, 1, [15, 16, 17, 18, 19, 20, 21], [100]), ...dive("2030-01-01", 1, 2, [22, 23, 24, 25, 26, 27, 28], [0]),
      ...dive("2030-01-02", 1, 1, ids, [50]), ...dive("2030-01-02", 1, 2, [8, 9, 10, 11, 12, 13, 14], [50]),
    ];
    const r = replay(ps, games, params);
    const find = (type: string, id: number, attempt: number) =>
      r.history.find((h) => h.date === "2030-01-02" && h.scope_type === type && h.scope_id === id && h.attempt === attempt)!;
    expect(find("competition", 1, 2).rating_before).not.toBe(find("competition", 2, 1).rating_before);
    expect(find("club", 31, 1).rating_before).toBe(find("competition", 1, 2).rating_before);
  });

  it("rejects bad game rows, prompts and params with clear errors", () => {
    const one = dive("2030-01-01", 1, 1, [1], [10]);
    const at = (g: Partial<Game>[]) => () => replay(prompts(2), g.map((x, i) => ({ ...one[0]!, prompt_id: i + 1, ...x })), params);
    expect(at([{}, { prompt_id: 1, attempt: 2 }])).toThrow(/duplicate rated game/);
    expect(at([{ points: 101 }])).toThrow(/outside 0..100/);
    expect(at([{ points: -5 }])).toThrow(/outside 0..100/);
    expect(at([{ points: NaN }])).toThrow(/finite/);
    expect(at([{ points: "10" as unknown as number }])).toThrow(/finite/);
    for (const bad of ["2030-1-1", "2030-01-01T00:00:00.000Z", "2030-01-01 ", "2030-02-30", "2031-02-29", new Date("2030-01-01")]) {
      expect(at([{ date: bad as string }])).toThrow(/YYYY-MM-DD|calendar/);
    }
    expect(at([{ date: "2032-02-29" }])).not.toThrow();
    expect(() => replay(prompts(1).map((p) => ({ ...p, club_ids: [5, 5] })), one, params)).toThrow(/club twice/);
    expect(() => replay(prompts(1).map((p) => ({ ...p, features: { log_recall_mass: NaN } })), one, params)).toThrow(/finite/);
    expect(() => replay(prompts(1), one, { ...params, tau: NaN })).toThrow(/params.tau/);
    const archive = [...one, { ...one[0]!, attempt: 2, rated: false }, { ...one[0]!, prompt_id: 2, points: 999, unanswered: true }];
    expect(() => replay(prompts(2), archive, params)).not.toThrow();
  });

  it("rejects games for unknown prompts and duplicate prompt ids", () => {
    expect(() => replay(prompts(1), dive("2030-01-01", 1, 1, [9], [10]), params)).toThrow(/unknown prompt/);
    expect(() => replay([...prompts(1), ...prompts(1)], [], params)).toThrow(/duplicate/);
  });
});
