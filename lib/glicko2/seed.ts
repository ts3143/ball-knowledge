/** A prompt's starting rating: a linear map from features frozen at approval (rarity values), fitted in the sim. */

import type { SeedParams } from "./types";

/** intercept + sum(coefficient_k * feature_k) + template_offset, summed in sorted coefficient-name order.
 * A coefficient without its feature throws: a prompt frozen without it cannot be seeded. */
export function seedRating(features: Readonly<Record<string, number>>, seed: SeedParams, templateOffset = 0.0): number {
  let total = seed.intercept;
  for (const name of Object.keys(seed.coefficients).sort()) {
    const feature = features[name];
    if (feature === undefined) throw new Error(`prompt has no feature ${JSON.stringify(name)}`);
    total += (seed.coefficients[name] as number) * feature;
  }
  return total + templateOffset;
}

export function seedRd(seed: SeedParams): number {
  return seed.rd;
}
