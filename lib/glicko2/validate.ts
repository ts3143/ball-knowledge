/** Input checks shared by the engine entry points. The Python reference (sim/rating/validate.py) rejects the same
 * inputs: dates must be real calendar dates in YYYY-MM-DD, numbers must be finite, results must lie in [0, 1], a
 * user plays a prompt once per date, and a prompt lists each club once. */

import type { Game, Params, Prompt } from "./types";

/** x as a number; anything that is not a finite number (strings, null, NaN, Infinity) throws. */
export function finite(x: unknown, name: string): number {
  if (typeof x !== "number" || !Number.isFinite(x)) {
    throw new Error(`${name} must be a finite number, got ${JSON.stringify(x) ?? String(x)}`);
  }
  return x;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** A real calendar date written exactly YYYY-MM-DD; "2030-1-1", timestamps, "2030-02-30" and Date objects throw. */
export function checkDate(d: unknown, name = "date"): string {
  if (typeof d !== "string" || !ISO_DATE.test(d)) {
    throw new Error(`${name} must be a YYYY-MM-DD string, got ${JSON.stringify(d) ?? String(d)}`);
  }
  const ms = Date.parse(`${d}T00:00:00Z`);
  if (Number.isNaN(ms) || new Date(ms).toISOString().slice(0, 10) !== d) {
    throw new Error(`${name} is not a real calendar date: ${d}`);
  }
  return d;
}

export function checkParams(params: Params): void {
  for (const name of [
    "initial_rating", "initial_rd", "initial_volatility", "tau", "volatility_epsilon", "child_scope_initial_rd",
    "game_weight", "uncertain_rd_threshold", "club_rating_min_prompts", "prompt_lock_answers", "points_scale",
  ] as const) {
    finite(params[name], `params.${name}`);
  }
  for (const name of [
    "initial_rd", "initial_volatility", "tau", "volatility_epsilon", "child_scope_initial_rd", "game_weight",
    "points_scale",
  ] as const) {
    if (params[name] <= 0) throw new Error(`params.${name} must be above 0, got ${params[name]}`);
  }
  finite(params.seed.intercept, "params.seed.intercept");
  finite(params.seed.rd, "params.seed.rd");
  for (const [k, v] of Object.entries(params.seed.coefficients)) finite(v, `params.seed.coefficients.${k}`);
}

export function checkPrompts(prompts: readonly Prompt[]): void {
  for (const p of prompts) {
    if (new Set(p.club_ids).size !== p.club_ids.length) {
      throw new Error(`prompt ${p.id} lists a club twice: ${JSON.stringify(p.club_ids)}`);
    }
    finite(p.template_offset, `prompt ${p.id} template_offset`);
    for (const [k, v] of Object.entries(p.features)) finite(v, `prompt ${p.id} feature ${k}`);
  }
}

/** Rated games only. An unanswered game scores 0 whatever its points say, so its points are not checked. */
export function checkGames(games: readonly Game[], pointsScale: number): void {
  const seen = new Set<string>();
  for (const g of games) {
    checkDate(g.date, `game date (user ${g.user_id}, prompt ${g.prompt_id})`);
    const key = `${g.user_id}|${g.date}|${g.prompt_id}`;
    if (seen.has(key)) {
      throw new Error(`duplicate rated game: user ${g.user_id} on ${g.date} prompt ${g.prompt_id} ` +
        "(one rated attempt per user per dive)");
    }
    seen.add(key);
    if (!g.unanswered) {
      const points = finite(g.points, `points (user ${g.user_id}, prompt ${g.prompt_id})`);
      if (points < 0 || points > pointsScale) {
        throw new Error(`points ${points} outside 0..${pointsScale} (user ${g.user_id}, prompt ${g.prompt_id})`);
      }
    }
  }
}
