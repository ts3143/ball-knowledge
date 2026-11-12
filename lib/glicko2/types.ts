/** Inputs and outputs of the engine. Ids are numbers; dates are ISO "YYYY-MM-DD" strings. Field names match the
 * Python reference and the golden file. */

export const SPORT = "sport";
export const COMPETITION = "competition";
export const CLUB = "club";
export type ScopeType = typeof SPORT | typeof COMPETITION | typeof CLUB;

const SCOPE_ORDER: Record<ScopeType, number> = { sport: 0, competition: 1, club: 2 };

/** [scope_type, scope_id]: the sport's id is its name ("football"), a competition's or club's is its entity id. */
export type Scope = readonly [ScopeType, number | string];

/** Sort comparator: sport, then competitions, then clubs, each by id. */
export function compareScopes(a: Scope, b: Scope): number {
  return SCOPE_ORDER[a[0]] - SCOPE_ORDER[b[0]] || compareIds(a[1], b[1]);
}

export function compareIds(a: number | string, b: number | string): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  const x = String(a);
  const y = String(b);
  return x < y ? -1 : x > y ? 1 : 0;
}

export function scopeKey(scope: Scope): string {
  return `${scope[0]}:${scope[1]}`;
}

export interface State {
  rating: number;
  rd: number;
  volatility: number;
  games: number;
  last_period: string | null;
}

export interface Prompt {
  id: number;
  competition_id: number;
  club_ids: readonly number[];
  features: Readonly<Record<string, number>>;
  template_offset: number;
  sport: string;
}

/** One prompt result of one rated attempt. Archive plays carry rated=false; `unanswered` scores 0 whatever
 * `points` says. */
export interface Game {
  user_id: number;
  date: string;
  attempt: number;
  prompt_id: number;
  points: number;
  rated: boolean;
  unanswered: boolean;
}

export interface PromptState {
  rating: number;
  rd: number;
  volatility: number;
  games: number;
  locked: boolean;
  last_period: string | null;
  seed_rating: number;
  seed_rd: number;
}

export interface HistoryRow {
  user_id: number;
  date: string;
  attempt: number;
  scope_type: ScopeType;
  scope_id: number | string;
  rating_before: number;
  rd_before: number;
  volatility_before: number;
  rating_after: number;
  rd_after: number;
  volatility_after: number;
  games: number;
  score_sum: number;
}

export interface UserScopeState {
  user_id: number;
  scope: Scope;
  state: State;
}

export interface ReplayResult {
  scopes: UserScopeState[];
  history: HistoryRow[];
  prompts: Map<number, PromptState>;
}

export interface SeedParams {
  intercept: number;
  rd: number;
  coefficients: Readonly<Record<string, number>>;
}

export interface Params {
  initial_rating: number;
  initial_rd: number; // also the cap for idle growth and for phi*
  initial_volatility: number;
  tau: number;
  volatility_epsilon: number;
  child_scope_initial_rd: number;
  game_weight: number;
  uncertain_rd_threshold: number;
  club_rating_min_prompts: number;
  prompt_lock_answers: number;
  points_scale: number; // points that make a result of 1.0 (the top tier's points)
  seed: SeedParams;
}

/** What one rating period needs. */
export type CoreParams = Pick<Params, "tau" | "volatility_epsilon" | "game_weight" | "initial_rd">;
