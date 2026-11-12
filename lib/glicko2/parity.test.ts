import { afterAll, describe, expect, it } from "vitest";
import golden from "../../sim/tests/glicko2_cases.json";
import {
  DegeneratePeriodError, ENGINE_VERSION, promptFeatures, ratePeriod, replay, type Game, type Opponent, type Params,
  type Prompt,
} from "./index";
import { GLICKMAN } from "./example";

interface CoreCase {
  name: string;
  params: { tau: number; volatility_epsilon: number; game_weight: number; initial_rd: number };
  input: { rating: number; rd: number; volatility: number; idle_periods: number; opponents: Opponent[] };
  expected?: { rating: number; rd: number; volatility: number };
  published?: { rating: number; rd: number; volatility: number };
  expected_error?: string;
}
interface FeatureCase {
  name: string;
  rarity: Record<string, unknown>;
  gate_results: Record<string, unknown>;
  expected?: { log_recall_mass: number; ln_n_eff: number };
  expected_error?: string;
}
const coreCases = golden.core as CoreCase[];
const featureCases = golden.features as FeatureCase[];

const TOLERANCE = golden.relative_tolerance;
const ABS_TOLERANCE = 1e-9; // for values at or near 0 (a relative difference means nothing there)
const EXACT_NUMBERS = new Set(["games", "attempt", "user_id", "prompt_id", "scope_id"]);

let maxRelative = 0;
let compared = 0;
const mismatches: string[] = [];

/** Numbers compare at relative TOLERANCE (absolute near 0); integer ids and counts, strings, booleans and
 * null compare exactly. Records the largest relative difference seen. */
function compare(actual: unknown, expected: unknown, path: string, key = ""): void {
  if (typeof expected === "number" && typeof actual === "number") {
    compared += 1;
    if (EXACT_NUMBERS.has(key)) {
      if (actual !== expected) mismatches.push(`${path}: ${actual} !== ${expected}`);
      return;
    }
    const diff = Math.abs(actual - expected);
    const scale = Math.max(Math.abs(actual), Math.abs(expected));
    if (scale > ABS_TOLERANCE) maxRelative = Math.max(maxRelative, diff / scale);
    if (!(diff <= TOLERANCE * scale || diff <= ABS_TOLERANCE)) mismatches.push(`${path}: ${actual} vs ${expected}`);
  } else if (Array.isArray(expected)) {
    if (!Array.isArray(actual) || actual.length !== expected.length) {
      mismatches.push(`${path}: array length differs`);
      return;
    }
    expected.forEach((e, i) => compare(actual[i], e, `${path}[${i}]`, key));
  } else if (expected !== null && typeof expected === "object") {
    const a = actual as Record<string, unknown>;
    const e = expected as Record<string, unknown>;
    if (Object.keys(a).sort().join() !== Object.keys(e).sort().join()) {
      mismatches.push(`${path}: keys ${Object.keys(a).sort()} vs ${Object.keys(e).sort()}`);
      return;
    }
    for (const k of Object.keys(e)) compare(a[k], e[k], `${path}/${k}`, k);
  } else if (actual !== expected) {
    mismatches.push(`${path}: ${JSON.stringify(actual)} !== ${JSON.stringify(expected)}`);
  }
}

afterAll(() => {
  console.log(`golden parity: ${compared} numbers compared, largest relative difference ${maxRelative.toExponential(3)}`);
});

describe("golden file", () => {
  it("has the engine version of the TypeScript port", () => {
    expect(golden.engine_version).toBe(ENGINE_VERSION);
    expect(ENGINE_VERSION).toBe("bk-glicko-2");
    expect(TOLERANCE).toBe(1e-6);
  });

  it("holds the expected number of cases", () => {
    expect(golden.core).toHaveLength(59);
    expect(coreCases.filter((c) => c.expected_error !== undefined)).toHaveLength(2);
    expect(golden.features).toHaveLength(10);
    expect(golden.replay).toHaveLength(9);
  });
});

describe("core cases", () => {
  it.each(coreCases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const params = { ...c.params };
    if (c.expected_error !== undefined) {
      expect(c.expected_error).toBe("degenerate");
      expect(() => ratePeriod(c.input.rating, c.input.rd, c.input.volatility, c.input.idle_periods,
        c.input.opponents, params)).toThrow(DegeneratePeriodError);
      return;
    }
    const out = ratePeriod(c.input.rating, c.input.rd, c.input.volatility, c.input.idle_periods, c.input.opponents,
      params);
    const before = mismatches.length;
    compare({ rating: out.rating, rd: out.rd, volatility: out.volatility }, c.expected, c.name);
    expect(mismatches.slice(before)).toEqual([]);
  });
});

describe("feature cases", () => {
  it.each(featureCases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    if (c.expected_error !== undefined) {
      expect(() => promptFeatures(c.rarity, c.gate_results)).toThrow();
      return;
    }
    const before = mismatches.length;
    compare(promptFeatures(c.rarity, c.gate_results), c.expected, c.name);
    expect(mismatches.slice(before)).toEqual([]);
  });
});

describe("Glickman's worked example", () => {
  it("matches the published values at the paper's precision", () => {
    const c = coreCases.find((x) => x.name === "glickman_example");
    expect(c).toBeDefined();
    const i = GLICKMAN.inputs;
    const out = ratePeriod(i.rating, i.rd, i.volatility, 0, i.opponents, c!.params);
    const tol = GLICKMAN.published_abs_tolerance;
    expect(Math.abs(out.rating - 1464.06)).toBeLessThanOrEqual(tol.rating);
    expect(Math.abs(out.rd - 151.52)).toBeLessThanOrEqual(tol.rd);
    expect(Math.abs(out.volatility - 0.05999)).toBeLessThanOrEqual(tol.volatility);
    expect(c!.published).toEqual(GLICKMAN.published);
  });
});

describe("replay cases", () => {
  it.each(golden.replay.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const result = replay(c.prompts as Prompt[], c.games as Game[], c.params as Params);
    const actual = {
      scopes: result.scopes.map((s) => ({
        user_id: s.user_id, scope_type: s.scope[0], scope_id: s.scope[1], ...s.state,
      })),
      history: result.history,
      prompts: [...result.prompts].map(([prompt_id, ps]) => ({ prompt_id, ...ps })),
    };
    const before = mismatches.length;
    compare(actual, c.expected, c.name);
    expect(mismatches.slice(before)).toEqual([]);
  });
});
