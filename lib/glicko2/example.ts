/** The engine in plain language: Glickman's example and a worked replay. Placeholder ids and names only.
 * Mirrors `python -m rating example`; example.test.ts prints it and checks it against the Python output.
 * Params are the chunk-1 defaults the Python example is run with (see example.expected.ts). */

import { ratePeriod } from "./core";
import { addDays, clubVisible, daysBetween, isUncertain, rdNow } from "./display";
import { replay } from "./engine";
import { CLUB, COMPETITION, SPORT, scopeKey, type Game, type Params, type Prompt, type Scope, type State } from "./types";

export const EXAMPLE_PARAMS: Params = {
  initial_rating: 1500.0,
  initial_rd: 350.0,
  initial_volatility: 0.06,
  tau: 0.5,
  volatility_epsilon: 0.000001,
  child_scope_initial_rd: 250.0,
  game_weight: 1.0,
  uncertain_rd_threshold: 110.0,
  club_rating_min_prompts: 20,
  prompt_lock_answers: 200,
  points_scale: 100.0,
  seed: { intercept: 1500.0, rd: 350.0, coefficients: { log_recall_mass: 0.0 } },
};

export const GLICKMAN = {
  inputs: {
    rating: 1500.0,
    rd: 200.0,
    volatility: 0.06,
    opponents: [
      { rating: 1400.0, rd: 30.0, score: 1.0 },
      { rating: 1550.0, rd: 100.0, score: 0.0 },
      { rating: 1700.0, rd: 300.0, score: 0.0 },
    ],
  },
  published: { rating: 1464.06, rd: 151.52, volatility: 0.05999 },
  published_abs_tolerance: { rating: 0.01, rd: 0.01, volatility: 0.00001 },
};

const SPORT_SCOPE: Scope = [SPORT, "football"];
const COMP_SCOPE: Scope = [COMPETITION, 1];
const CLUB_SCOPE: Scope = [CLUB, 101];
const NAMES: Record<string, string> = {
  [scopeKey(SPORT_SCOPE)]: "Football (sport)",
  [scopeKey(COMP_SCOPE)]: "Competition A",
  [scopeKey(CLUB_SCOPE)]: "Club A",
};
const SCOPE_ORDER: Scope[] = [SPORT_SCOPE, COMP_SCOPE, CLUB_SCOPE];
// Points per prompt for each of the five dives (a tier's points, 15 for a Hipster Pick, 0 for a miss).
const DIVES = [
  [10, 25, 0, 10, 50, 10, 25],
  [25, 50, 10, 75, 25, 10, 0],
  [50, 75, 25, 100, 50, 25, 10],
  [75, 50, 100, 25, 50, 75, 10],
  [100, 75, 50, 100, 25, 75, 50],
];
const DEMO_SEED_SLOPE = 30.0; // demo only: spreads the prompts' starting ratings so the opponents differ

const f = (x: number, width: number, digits: number): string => x.toFixed(digits).padStart(width);
const sum = (xs: number[]): number => xs.reduce((a, b) => a + b, 0);

export function glickmanLines(): string[] {
  const i = GLICKMAN.inputs;
  const out = ratePeriod(i.rating, i.rd, i.volatility, 0, i.opponents, EXAMPLE_PARAMS);
  const lines = [
    "(a) Glickman's worked example",
    "    A player rated 1500 (RD 200, volatility 0.06) plays three opponents: a win against 1400 (RD 30),",
    "    a loss against 1550 (RD 100) and a loss against 1700 (RD 300). tau = 0.5, standard weight 1.",
    `    ${"".padEnd(12)}${"ours".padStart(14)}${"published".padStart(12)}`,
  ];
  const rows: [string, number, number, number][] = [
    ["new rating", out.rating, GLICKMAN.published.rating, 2],
    ["new RD", out.rd, GLICKMAN.published.rd, 2],
    ["volatility", out.volatility, GLICKMAN.published.volatility, 5],
  ];
  for (const [label, ours, published, digits] of rows) {
    lines.push(`    ${label.padEnd(12)}${f(ours, 14, 6)}${f(published, 12, digits)}`);
  }
  lines.push(`    (volatility step converged in ${out.iterations} iterations)`);
  return lines;
}

export function workedReplayLines(params: Params = EXAMPLE_PARAMS): string[] {
  const d0 = "2026-01-01";
  const days = [0, 1, 8, 9, 10].map((n) => addDays(d0, n));
  // Dives 1-2: no club tags. Dive 3: six of seven prompts tag Club A, dives 4-5: all seven do.
  const clubPrompts: Record<number, number> = { 2: 6, 3: 7, 4: 7 };
  const prompts: Prompt[] = [];
  const games: Game[] = [];
  DIVES.forEach((points, d) => {
    for (let k = 0; k < 7; k++) {
      const pid = 100 * (d + 1) + k;
      const tagged = k < (clubPrompts[d] ?? 0);
      prompts.push({
        id: pid,
        competition_id: 1,
        club_ids: tagged ? [101] : [],
        features: { log_recall_mass: -2.0 + 0.5 * (k % 5) },
        template_offset: 0.0,
        sport: "football",
      });
      games.push({
        user_id: 1, date: days[d] as string, attempt: 1, prompt_id: pid, points: points[k] as number,
        rated: true, unanswered: false,
      });
    }
  });
  const slopes = Object.fromEntries(
    Object.entries(params.seed.coefficients).map(([k, v]) => [k, v || DEMO_SEED_SLOPE]),
  );
  const demo: Params = { ...params, seed: { ...params.seed, coefficients: slopes } };
  const result = replay(prompts, games, demo);

  const lines = [
    "",
    "(b) A worked replay: one synthetic player, five dives in Competition A",
    "    Rating and RD are shown before and after each dive. Every dive is one rating period for each scope",
    `    it touches. Uncertain marker: RD above ${demo.uncertain_rd_threshold}. A club rating is shown from ` +
      `${demo.club_rating_min_prompts} prompts.`,
    `    Prompt ratings here start at ${demo.seed.intercept} + ${DEMO_SEED_SLOPE} x log_recall_mass ` +
      "(demo values, not tuned).",
  ];
  const byKey = new Map(result.history.map((h) => [`${h.date}|${h.scope_type}:${h.scope_id}`, h]));
  const promptsSeen = new Map<string, number>(); // prompts per scope so far (club visibility)
  days.forEach((day, idx) => {
    const n = idx + 1;
    const gap = n > 1 ? daysBetween(days[n - 2] as string, day) - 1 : 0;
    const note = gap > 0 ? `   <- ${gap} dive dates skipped, so RD grows before the dive` : "";
    lines.push("", `  Dive ${n}, ${day}: ${sum(DIVES[n - 1] as number[])} points of 700${note}`);
    for (const scope of SCOPE_ORDER) {
      const key = scopeKey(scope);
      const h = byKey.get(`${day}|${key}`);
      const name = (NAMES[key] as string).padEnd(18);
      if (h === undefined) {
        if (key === scopeKey(CLUB_SCOPE)) lines.push(`    ${name} not touched yet (no club prompt)`);
        continue;
      }
      const first = key === scopeKey(CLUB_SCOPE) && day === days[2]
        ? ` (new: starts at its parent's rating, RD at least ${demo.child_scope_initial_rd})` : "";
      const st: State = {
        rating: h.rating_after, rd: h.rd_after, volatility: h.volatility_after, games: h.games, last_period: day,
      };
      const total = (promptsSeen.get(key) ?? 0) + h.games;
      promptsSeen.set(key, total);
      const flag = isUncertain(st, day, demo) ? "uncertain" : "settled";
      let extra = "";
      if (scope[0] === CLUB) {
        extra = total >= demo.club_rating_min_prompts
          ? ", rating shown"
          : `, rating hidden (${total} of ${demo.club_rating_min_prompts} prompts)`;
      }
      lines.push(`    ${name} rating ${f(h.rating_before, 7, 1)} -> ${f(h.rating_after, 7, 1)}   ` +
        `RD ${f(h.rd_before, 6, 1)} -> ${f(h.rd_after, 6, 1)}   ${flag}${extra}${first}`);
    }
  });
  const last = days[days.length - 1] as string;
  const later = addDays(last, 7);
  lines.push("", `  A week after dive 5 (${later}), RD if the player returned:`);
  for (const scope of SCOPE_ORDER) {
    const st = result.scopes.find((s) => s.user_id === 1 && scopeKey(s.scope) === scopeKey(scope))?.state as State;
    const vis = scope[0] === CLUB ? `, rating ${clubVisible(st, demo) ? "shown" : "hidden"}` : "";
    lines.push(`    ${(NAMES[scopeKey(scope)] as string).padEnd(18)} RD ${f(st.rd, 6, 1)} -> ` +
      `${f(rdNow(st, later, demo), 6, 1)}   ${isUncertain(st, later, demo) ? "uncertain" : "settled"}${vis}`);
  }
  return lines;
}

export function exampleLines(): string[] {
  return [...glickmanLines(), ...workedReplayLines()];
}
