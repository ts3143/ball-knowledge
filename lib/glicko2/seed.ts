/** A prompt's starting rating: a linear map from features frozen at approval (rarity values), fitted in the sim. */

import { finite } from "./validate";
import type { SeedParams } from "./types";

type Json = Readonly<Record<string, unknown>>;

/** The seed features of a prompts row, from its `rarity` and `gate_results` jsonb: log_recall_mass is
 * rarity.log_recall_mass and ln_n_eff is ln(gate_results.tiers.n_eff). A missing, non-numeric or non-finite value,
 * or an n_eff that is not above 0, throws: a prompt without them cannot be seeded. */
export function promptFeatures(rarity: Json | null | undefined, gateResults: Json | null | undefined):
    { log_recall_mass: number; ln_n_eff: number } {
  const tiers = (gateResults?.tiers ?? null) as Json | null;
  const recall = finite(rarity?.log_recall_mass, "rarity.log_recall_mass");
  const nEff = finite(tiers?.n_eff, "gate_results.tiers.n_eff");
  if (nEff <= 0) throw new Error(`gate_results.tiers.n_eff must be above 0, got ${nEff}`);
  return { log_recall_mass: recall, ln_n_eff: Math.log(nEff) };
}

/** intercept + sum(coefficient_k * feature_k) + template_offset, summed in sorted coefficient-name order.
 * A coefficient without its feature, or a non-finite feature or offset, throws: a prompt frozen without them
 * cannot be seeded. */
export function seedRating(features: Readonly<Record<string, number>>, seed: SeedParams, templateOffset = 0.0): number {
  finite(templateOffset, "template_offset");
  let total = seed.intercept;
  for (const name of Object.keys(seed.coefficients).sort()) {
    if (!(name in features)) throw new Error(`prompt has no feature ${JSON.stringify(name)}`);
    total += (seed.coefficients[name] as number) * finite(features[name], `feature ${name}`);
  }
  return total + templateOffset;
}

export function seedRd(seed: SeedParams): number {
  return seed.rd;
}
