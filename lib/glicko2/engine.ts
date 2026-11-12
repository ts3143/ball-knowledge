/** Replay of rated games into ratings: one rating period per (user, scope, dive date), and one per prompt.
 *
 * A club's parent competition is chosen per user: the competition of the user's own first rated prompt that tags the
 * club (the lowest competition id on a date tie), so one user's games never change another user's ratings.
 *
 * Dates run in ascending order. Within a date every user is independent, and prompts are settled after all users
 * from the player states at the start of the date, so a prompt's rating is frozen for the whole date. After each
 * attempt a user's period for the date is recomputed from the state before the date using all the date's games so
 * far (in prompt order), so two attempts equal one batch. Nothing reads a clock.
 */

import { idleRd, ratePeriod, type Opponent } from "./core";
import { idlePeriods } from "./display";
import { seedRating, seedRd } from "./seed";
import { checkGames, checkParams, checkPrompts } from "./validate";
import {
  CLUB,
  COMPETITION,
  SPORT,
  compareScopes,
  scopeKey,
  type Game,
  type HistoryRow,
  type Params,
  type Prompt,
  type PromptState,
  type ReplayResult,
  type Scope,
  type State,
  type UserScopeState,
} from "./types";

const byNumber = (a: number, b: number): number => a - b;
const compareDates = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

function push<K, V>(map: Map<K, V[]>, key: K, value: V): void {
  const list = map.get(key);
  if (list === undefined) map.set(key, [value]);
  else list.push(value);
}

function sortedKeys<V>(map: Map<number, V>): number[] {
  return [...map.keys()].sort(byNumber);
}

/** "user|club" -> parent competition: that of the user's first rated prompt that tags the club, the lowest
 * competition id on a date tie. */
export function clubParents(prompts: Map<number, Prompt>, games: readonly Game[]): Map<string, number> {
  const best = new Map<string, [string, number]>();
  for (const g of games) {
    const p = prompts.get(g.prompt_id) as Prompt;
    for (const club of p.club_ids) {
      const key = `${g.user_id}|${club}`;
      const current = best.get(key);
      if (current === undefined || compareDates(g.date, current[0]) < 0 ||
          (g.date === current[0] && p.competition_id < current[1])) {
        best.set(key, [g.date, p.competition_id]);
      }
    }
  }
  return new Map([...best].map(([key, [, competition]]) => [key, competition]));
}

export function promptScopes(p: Prompt): Scope[] {
  return [
    [SPORT, p.sport],
    [COMPETITION, p.competition_id],
    ...[...p.club_ids].sort(byNumber).map((c): Scope => [CLUB, c]),
  ];
}

// One row per game that settles a prompt: user, attempt, result, and the player's competition-scope rating and
// RD at the start of the date.
type SettleRow = { user: number; attempt: number; result: number; rating: number; rd: number };

export function replay(prompts: readonly Prompt[], games: readonly Game[], params: Params): ReplayResult {
  const promptById = new Map<number, Prompt>();
  for (const p of prompts) {
    if (promptById.has(p.id)) throw new Error(`duplicate prompt id ${p.id}`);
    promptById.set(p.id, p);
  }
  checkParams(params);
  checkPrompts(prompts);
  const rated = games.filter((g) => g.rated); // archive plays never enter the engine
  for (const g of rated) {
    if (!promptById.has(g.prompt_id)) throw new Error(`game for unknown prompt ${g.prompt_id}`);
  }
  checkGames(rated, params.points_scale);

  const byDate = new Map<string, Game[]>();
  for (const g of rated) push(byDate, g.date, g);
  const parentClub = clubParents(promptById, rated);
  const sportOf = new Map<number, string>(); // competition -> sport
  for (const pid of sortedKeys(promptById)) {
    const p = promptById.get(pid) as Prompt;
    const known = sportOf.get(p.competition_id);
    if (known === undefined) sportOf.set(p.competition_id, p.sport);
    else if (known !== p.sport) throw new Error(`competition ${p.competition_id} appears in two sports`);
  }

  const parentOf = (uid: number, scope: Scope): Scope | null => {
    if (scope[0] === CLUB) return [COMPETITION, parentClub.get(`${uid}|${scope[1]}`) as number];
    if (scope[0] === COMPETITION) return [SPORT, sportOf.get(scope[1] as number) as string];
    return null;
  };

  const userScopes = new Map<number, Map<string, [Scope, State]>>();
  const promptStates = new Map<number, PromptState>();
  const history: HistoryRow[] = [];

  for (const day of [...byDate.keys()].sort(compareDates)) {
    const dayGames = byDate.get(day) as Game[];
    const pids = [...new Set(dayGames.map((g) => g.prompt_id))].sort(byNumber);
    for (const pid of pids) {
      if (!promptStates.has(pid)) {
        const p = promptById.get(pid) as Prompt;
        const seed = seedRating(p.features, params.seed, p.template_offset);
        promptStates.set(pid, {
          rating: seed,
          rd: seedRd(params.seed),
          volatility: params.initial_volatility,
          games: 0,
          locked: false,
          last_period: null,
          seed_rating: seed,
          seed_rd: seedRd(params.seed),
        });
      }
    }
    const byUser = new Map<number, Game[]>();
    for (const g of dayGames) push(byUser, g.user_id, g);
    const settle = new Map<number, SettleRow[]>();
    for (const uid of sortedKeys(byUser)) {
      let states = userScopes.get(uid);
      if (states === undefined) {
        states = new Map();
        userScopes.set(uid, states);
      }
      playUser(uid, day, byUser.get(uid) as Game[], promptById, promptStates, states, parentOf, params, history,
        settle);
    }
    for (const pid of sortedKeys(settle)) {
      settlePrompt(pid, day, settle.get(pid) as SettleRow[], promptStates, params);
    }
  }

  const scopes: UserScopeState[] = [];
  for (const uid of sortedKeys(userScopes)) {
    const entries = [...(userScopes.get(uid) as Map<string, [Scope, State]>).values()];
    entries.sort((a, b) => compareScopes(a[0], b[0]));
    for (const [scope, state] of entries) scopes.push({ user_id: uid, scope, state });
  }
  const prompts_ = new Map(sortedKeys(promptStates).map((pid) => [pid, promptStates.get(pid) as PromptState]));
  return { scopes, history, prompts: prompts_ };
}

/** All of one user's periods on one date. `states` is updated in place. */
function playUser(
  uid: number,
  day: string,
  games: Game[],
  promptById: Map<number, Prompt>,
  promptStates: Map<number, PromptState>,
  states: Map<string, [Scope, State]>,
  parentOf: (uid: number, scope: Scope) => Scope | null,
  params: Params,
  history: HistoryRow[],
  settle: Map<number, SettleRow[]>,
): void {
  const pre = new Map(states); // state before the date; every start of the date derives from it
  const starts = new Map<string, State>();

  const start = (scope: Scope): State => {
    const key = scopeKey(scope);
    const cached = starts.get(key);
    if (cached !== undefined) return cached;
    let result: State;
    const before = pre.get(key);
    const parent = before === undefined ? parentOf(uid, scope) : null;
    if (before !== undefined) {
      const st = before[1];
      if (st.last_period === null) throw new Error("stored state without last_period");
      const idle = idlePeriods(st.last_period, day);
      result = {
        rating: st.rating,
        rd: idleRd(st.rd, st.volatility, idle, params.initial_rd),
        volatility: st.volatility,
        games: st.games,
        last_period: st.last_period,
      };
    } else if (parent !== null) {
      const p = start(parent); // the parent's state at the start of the date, never its updated one
      result = {
        rating: p.rating,
        rd: Math.max(params.child_scope_initial_rd, p.rd),
        volatility: params.initial_volatility,
        games: 0,
        last_period: null,
      };
    } else {
      result = {
        rating: params.initial_rating,
        rd: params.initial_rd,
        volatility: params.initial_volatility,
        games: 0,
        last_period: null,
      };
    }
    starts.set(key, result);
    return result;
  };

  const inPeriod = new Map<string, [number, Opponent][]>();
  const current = new Map<string, [Scope, State]>();
  const attempts = new Map<number, Game[]>();
  for (const g of games) push(attempts, g.attempt, g);
  for (const attempt of sortedKeys(attempts)) {
    const touched = new Map<string, Scope>();
    const attemptGames = [...(attempts.get(attempt) as Game[])].sort((a, b) => a.prompt_id - b.prompt_id);
    for (const g of attemptGames) {
      const p = promptById.get(g.prompt_id) as Prompt;
      const ps = promptStates.get(g.prompt_id) as PromptState;
      const result = g.unanswered ? 0.0 : g.points / params.points_scale;
      const opp: Opponent = { rating: ps.rating, rd: ps.rd, score: result };
      for (const scope of promptScopes(p)) {
        push(inPeriod, scopeKey(scope), [g.prompt_id, opp]);
        touched.set(scopeKey(scope), scope);
      }
      const compStart = start([COMPETITION, p.competition_id]);
      push(settle, g.prompt_id, { user: uid, attempt, result, rating: compStart.rating, rd: compStart.rd });
    }
    for (const scope of [...touched.values()].sort(compareScopes)) {
      const key = scopeKey(scope);
      const st = start(scope);
      const before = current.get(key)?.[1] ?? st;
      const period = [...(inPeriod.get(key) as [number, Opponent][])].sort((a, b) => a[0] - b[0]); // stable
      const opponents = period.map(([, o]) => o);
      const out = ratePeriod(st.rating, st.rd, st.volatility, 0, opponents, params);
      current.set(key, [scope, {
        rating: out.rating,
        rd: out.rd,
        volatility: out.volatility,
        games: st.games + opponents.length,
        last_period: day,
      }]);
      let scoreSum = 0.0;
      for (const o of opponents) scoreSum += o.score;
      history.push({
        user_id: uid,
        date: day,
        attempt,
        scope_type: scope[0],
        scope_id: scope[1],
        rating_before: before.rating,
        rd_before: before.rd,
        volatility_before: before.volatility,
        rating_after: out.rating,
        rd_after: out.rd,
        volatility_after: out.volatility,
        games: opponents.length,
        score_sum: scoreSum,
      });
    }
  }
  for (const [key, value] of current) states.set(key, value);
}

/** Date close: one period for the prompt against its players (by user, then attempt); its score is 1 - result. */
function settlePrompt(
  pid: number,
  day: string,
  rows: SettleRow[],
  promptStates: Map<number, PromptState>,
  params: Params,
): void {
  const ps = promptStates.get(pid) as PromptState;
  if (ps.locked) return;
  const opponents: Opponent[] = [...rows]
    .sort((a, b) => a.user - b.user || a.attempt - b.attempt)
    .map((r) => ({ rating: r.rating, rd: r.rd, score: 1.0 - r.result }));
  const out = ratePeriod(ps.rating, ps.rd, ps.volatility, 0, opponents, params); // prompts get no idle growth
  const games = ps.games + opponents.length;
  promptStates.set(pid, {
    rating: out.rating,
    rd: out.rd,
    volatility: out.volatility,
    games,
    locked: games >= params.prompt_lock_answers,
    last_period: day,
    seed_rating: ps.seed_rating,
    seed_rd: ps.seed_rd,
  });
}
