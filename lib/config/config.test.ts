import { describe, expect, it } from "vitest";
import { buildParams, loadTunables, matchingConfig, paramsHash, validateTunables } from "./index";

describe("config", () => {
  const t = loadTunables();

  it("loads tunables.toml and builds params like sim/rating/params.py", () => {
    const p = buildParams(t);
    expect(p.initial_rating).toBe(1500);
    expect(p.points_scale).toBe(Math.max(...t.tiers.points));
    expect(p.points_scale).toBe(100);
    expect(p.seed.coefficients.ln_n_eff).toBeCloseTo(36.3276);
    expect(p.club_rating_min_prompts).toBe(10);
  });

  it("is cached per process", () => {
    expect(loadTunables()).toBe(loadTunables());
  });

  it("paramsHash is stable, 64 hex chars and sensitive to values", () => {
    const p = buildParams(t);
    expect(paramsHash(p)).toMatch(/^[0-9a-f]{64}$/);
    expect(paramsHash({ ...p })).toBe(paramsHash(p));
    expect(paramsHash({ ...p, tau: 0.6 })).not.toBe(paramsHash(p));
  });

  it("paramsHash ignores key order", () => {
    const p = buildParams(t);
    const reordered = Object.fromEntries(Object.entries(p).reverse()) as typeof p;
    expect(paramsHash(reordered)).toBe(paramsHash(p));
  });

  it("validation names a missing key", () => {
    const bad = structuredClone(t);
    delete bad.matching.partial_min_chars;
    expect(() => validateTunables(bad)).toThrow(/\[matching\]\.partial_min_chars/);
  });

  it("validation names a missing section", () => {
    const bad = structuredClone(t);
    delete bad.dive;
    expect(() => validateTunables(bad)).toThrow(/\[dive\]/);
  });

  it("matchingConfig returns the [matching] values", () => {
    expect(matchingConfig(t).max_input_chars).toBe(100);
  });
});
