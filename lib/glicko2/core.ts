/** One Glicko-2 rating period for one player (Glickman, "Example of the Glicko-2 system"), pure functions.
 * Ratings are on the Glicko-1 scale in and out. Two additions to the paper:
 *   - game_weight w: v = 1 / sum(w g^2 E (1-E)), delta = v * sum(w g (s - E)). w = 1 is standard Glicko-2.
 *   - phi* (the pre-period deviation) and idle growth are capped at initial_rd.
 * Sums run left to right in a plain loop, in the caller's game order, exactly as in the Python reference. */

import type { CoreParams } from "./types";

export const SCALE = 173.7178; // Glicko-2 scale factor (400 / ln 10, as rounded in the paper)
export const CENTER = 1500.0;
export const MAX_ITERATIONS = 100;
const PI2 = Math.PI * Math.PI;

export class VolatilityError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "VolatilityError";
  }
}

/** Every game of the period has an expected result of exactly 0 or 1 (a rating gap beyond about 6000 points), so
 * the games carry no information (v is infinite) and the update is undefined. Glickman's system has no rule for it;
 * we refuse rather than invent one. Real ratings stay far from this. */
export class DegeneratePeriodError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DegeneratePeriodError";
  }
}

export interface Opponent {
  rating: number;
  rd: number;
  score: number; // this player's result against the opponent, 0..1
}

export interface Rated {
  rating: number;
  rd: number;
  volatility: number;
  iterations: number; // volatility-step iterations (0 when there were no games)
  bracket: "log" | "scan" | ""; // how the volatility search bracket was found ("" without games)
}

/** RD after `periods` rating periods without games: phi^2 grows by volatility^2 each, capped at initial_rd. */
export function idleRd(rd: number, volatility: number, periods: number, initialRd: number): number {
  if (periods <= 0) return rd;
  const phi = rd / SCALE;
  const grown = Math.sqrt(phi * phi + periods * volatility * volatility);
  return Math.min(grown, initialRd / SCALE) * SCALE;
}

function g(phi: number): number {
  return 1.0 / Math.sqrt(1.0 + (3.0 * phi * phi) / PI2);
}

function expected(mu: number, muJ: number, gJ: number): number {
  return 1.0 / (1.0 + Math.exp(-gJ * (mu - muJ)));
}

/** Step 5: the Illinois algorithm. Returns [sigma', iterations, bracket]. */
export function newVolatility(
  phi: number,
  volatility: number,
  delta: number,
  v: number,
  tau: number,
  epsilon: number,
): [number, number, "log" | "scan"] {
  const a = Math.log(volatility * volatility);
  const phi2 = phi * phi;
  const delta2 = delta * delta;
  const tau2 = tau * tau;

  const f = (x: number): number => {
    const ex = Math.exp(x);
    const d = phi2 + v + ex;
    return (ex * (delta2 - phi2 - v - ex)) / (2.0 * d * d) - (x - a) / tau2;
  };

  let iterations = 0;
  let bigA = a;
  let bigB: number;
  let bracket: "log" | "scan";
  if (delta2 > phi2 + v) {
    bigB = Math.log(delta2 - phi2 - v);
    bracket = "log";
  } else {
    bracket = "scan";
    let k = 1;
    while (f(a - k * tau) < 0.0) {
      k += 1;
      iterations += 1;
      if (iterations > MAX_ITERATIONS) {
        throw new VolatilityError(`bracketing took more than ${MAX_ITERATIONS} steps`);
      }
    }
    bigB = a - k * tau;
  }
  let fA = f(bigA);
  let fB = f(bigB);
  while (Math.abs(bigB - bigA) > epsilon) {
    iterations += 1;
    if (iterations > MAX_ITERATIONS) {
      throw new VolatilityError(`volatility step took more than ${MAX_ITERATIONS} iterations`);
    }
    const bigC = bigA + ((bigA - bigB) * fA) / (fB - fA);
    const fC = f(bigC);
    if (fC * fB <= 0.0) {
      // <= so an exact root (fC == 0) ends the search
      bigA = bigB;
      fA = fB;
    } else {
      fA = fA / 2.0;
    }
    bigB = bigC;
    fB = fC;
  }
  return [Math.exp(bigA / 2.0), iterations, bracket];
}

/** Idle growth for `idlePeriods` empty periods, then one period against `opponents` (in the caller's order).
 * Without opponents this is only the idle growth. */
export function ratePeriod(
  rating: number,
  rd: number,
  volatility: number,
  idlePeriods: number,
  opponents: readonly Opponent[],
  params: CoreParams,
): Rated {
  rd = idleRd(rd, volatility, idlePeriods, params.initial_rd);
  if (opponents.length === 0) return { rating, rd, volatility, iterations: 0, bracket: "" };
  const w = params.game_weight;
  const mu = (rating - CENTER) / SCALE;
  const phi = rd / SCALE;
  let invV = 0.0; // sum of w g^2 E (1-E)
  let gain = 0.0; // sum of w g (s - E)
  for (const o of opponents) {
    const go = g(o.rd / SCALE);
    const e = expected(mu, (o.rating - CENTER) / SCALE, go);
    invV += w * go * go * e * (1.0 - e);
    gain += w * go * (o.score - e);
  }
  if (!(invV > 0.0)) {
    throw new DegeneratePeriodError(`expected results saturate (rating ${rating}, RD ${rd}): no information to rate`);
  }
  const v = 1.0 / invV;
  const delta = v * gain;
  const [sigma, iterations, bracket] = newVolatility(phi, volatility, delta, v, params.tau, params.volatility_epsilon);
  const phiStar = Math.min(Math.sqrt(phi * phi + sigma * sigma), params.initial_rd / SCALE);
  const phiNew = 1.0 / Math.sqrt(1.0 / (phiStar * phiStar) + 1.0 / v);
  const muNew = mu + phiNew * phiNew * gain;
  return { rating: SCALE * muNew + CENTER, rd: SCALE * phiNew, volatility: sigma, iterations, bracket };
}
