/** What a player sees: RD today (computed, never stored), the uncertain marker, club visibility. */

import { idleRd } from "./core";
import { checkDate } from "./validate";
import type { Params, State } from "./types";

const MS_PER_DAY = 86_400_000;

/** Whole days from ISO date `a` to ISO date `b`. */
export function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${checkDate(b)}T00:00:00Z`) - Date.parse(`${checkDate(a)}T00:00:00Z`)) / MS_PER_DAY);
}

/** ISO date `days` after ISO date `d`. */
export function addDays(d: string, days: number): string {
  return new Date(Date.parse(`${checkDate(d)}T00:00:00Z`) + days * MS_PER_DAY).toISOString().slice(0, 10);
}

/** Dive dates skipped between lastPeriod and `today`: none when today is the next day. */
export function idlePeriods(lastPeriod: string, today: string): number {
  return Math.max(0, daysBetween(lastPeriod, today) - 1);
}

/** RD with idle growth up to `today`, as the player would start a dive on that date. */
export function rdNow(state: State | null, today: string, params: Params): number {
  if (state === null || state.last_period === null) return params.initial_rd;
  return idleRd(state.rd, state.volatility, idlePeriods(state.last_period, today), params.initial_rd);
}

export function isUncertain(state: State | null, today: string, params: Params): boolean {
  return rdNow(state, today, params) > params.uncertain_rd_threshold;
}

export function clubVisible(state: State | null, params: Params): boolean {
  return state !== null && state.games >= params.club_rating_min_prompts;
}
