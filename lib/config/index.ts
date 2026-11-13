import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { parse } from "smol-toml";
import type { Params } from "@/lib/glicko2";

export type Tunables = Record<string, any>;

export const TUNABLES_PATH = resolve(process.cwd(), "tunables.toml");

interface CacheEntry {
  mtimeMs: number;
  tree: Tunables;
}

const cache = new Map<string, CacheEntry>();

/**
 * Reads tunables.toml and caches it per process. Outside production the file's mtime is checked on every call, so an
 * edit applies on the next request without a restart (T wants to change the timer and test quickly). In production
 * the file is read once. A file that fails validation throws; the previous good copy is not used.
 */
export function loadTunables(path: string = TUNABLES_PATH): Tunables {
  const hit = cache.get(path);
  if (production()) {
    if (hit) return hit.tree;
  } else if (hit && statSync(/*turbopackIgnore: true*/ path).mtimeMs === hit.mtimeMs) {
    return hit.tree;
  }
  const mtimeMs = statSync(/*turbopackIgnore: true*/ path).mtimeMs;
  const tree = parse(readFileSync(/*turbopackIgnore: true*/ path, "utf8")) as Tunables;
  validateTunables(tree);
  cache.set(path, { mtimeMs, tree });
  return tree;
}

const production = () => process.env.NODE_ENV === "production";

export function reloadTunables(): Tunables {
  cache.delete(TUNABLES_PATH);
  return loadTunables();
}

function need(tree: Tunables, section: string, keys: string[]): void {
  const s = tree[section];
  if (s === undefined || typeof s !== "object") throw new Error(`tunables: missing section [${section}]`);
  for (const k of keys) {
    if (s[k] === undefined) throw new Error(`tunables: missing [${section}].${k}`);
  }
}

const num = (v: unknown) => typeof v === "number" && Number.isFinite(v);

/** Checks the sections the app needs. Throws a message naming the first problem. */
export function validateTunables(tree: Tunables): void {
  need(tree, "dive", ["prompts_per_dive", "seconds_per_prompt", "grace_seconds", "late_finish_minutes"]);
  need(tree, "tiers", ["names", "cumulative_share", "points", "hipster_pick_points"]);
  need(tree, "glicko", [
    "initial_rating", "initial_rd", "initial_volatility", "tau", "volatility_epsilon", "child_scope_initial_rd",
    "game_weight", "uncertain_rd_threshold", "club_rating_min_prompts", "prompt_lock_answers", "prompt_seed",
  ]);
  need(tree, "matching", [
    "fuzzy_one_edit_min_chars", "fuzzy_two_edits_min_chars", "partial_min_chars", "derived_surname_min_chars",
    "max_input_chars", "review_candidates_max",
  ]);
  need(tree, "competitions", []);
  const t = tree.tiers;
  if (!Array.isArray(t.points) || t.points.length === 0 || !t.points.every(num)) {
    throw new Error("tunables: [tiers].points must be a non-empty list of numbers");
  }
  for (const [k, v] of Object.entries(tree.matching)) {
    if (!num(v) || (v as number) < 0) throw new Error(`tunables: [matching].${k} must be a non-negative number`);
  }
  if (tree.matching.fuzzy_one_edit_min_chars > tree.matching.fuzzy_two_edits_min_chars) {
    throw new Error("tunables: fuzzy_one_edit_min_chars must not exceed fuzzy_two_edits_min_chars");
  }
  const seed = tree.glicko.prompt_seed;
  if (!num(seed.intercept) || !num(seed.rd) || typeof seed.coefficients !== "object") {
    throw new Error("tunables: [glicko.prompt_seed] needs intercept, rd and coefficients");
  }
}

/** Mirrors sim/rating/params.py Params.from_tunables. points_scale is the top tier's points. */
export function buildParams(tunables: Tunables = loadTunables()): Params {
  const g = tunables.glicko;
  const s = g.prompt_seed;
  const coefficients: Record<string, number> = {};
  for (const [k, v] of Object.entries(s.coefficients)) coefficients[k] = Number(v);
  return {
    initial_rating: Number(g.initial_rating),
    initial_rd: Number(g.initial_rd),
    initial_volatility: Number(g.initial_volatility),
    tau: Number(g.tau),
    volatility_epsilon: Number(g.volatility_epsilon),
    child_scope_initial_rd: Number(g.child_scope_initial_rd),
    game_weight: Number(g.game_weight),
    uncertain_rd_threshold: Number(g.uncertain_rd_threshold),
    club_rating_min_prompts: Math.trunc(Number(g.club_rating_min_prompts)),
    prompt_lock_answers: Math.trunc(Number(g.prompt_lock_answers)),
    points_scale: Math.max(...(tunables.tiers.points as number[]).map(Number)),
    seed: { intercept: Number(s.intercept), rd: Number(s.rd), coefficients },
  };
}

function canonical(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  if (v && typeof v === "object") {
    const o = v as Record<string, unknown>;
    return `{${Object.keys(o).sort().map((k) => `${JSON.stringify(k)}:${canonical(o[k])}`).join(",")}}`;
  }
  return JSON.stringify(v);
}

/** sha256 (hex) of the canonical JSON of the params: sorted keys, no whitespace. */
export function paramsHash(params: Params): string {
  return createHash("sha256").update(canonical(params)).digest("hex");
}

export interface MatchingConfig {
  fuzzy_one_edit_min_chars: number;
  fuzzy_two_edits_min_chars: number;
  partial_min_chars: number;
  derived_surname_min_chars: number;
  max_input_chars: number;
  review_candidates_max: number;
}

export function matchingConfig(tunables: Tunables = loadTunables()): MatchingConfig {
  return { ...tunables.matching } as MatchingConfig;
}
