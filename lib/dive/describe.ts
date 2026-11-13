// Player-facing wording for a result. Pure and shared by the play screen and the summary.
import type { PlayerResult } from "./service-types";

export function describeResult(r: PlayerResult): string {
  switch (r.verdict) {
    case "counted": {
      const label = r.hipster_pick ? `${r.tier_name}, Hipster Pick` : (r.tier_name ?? "");
      return `Counted: ${r.matched_name} — ${label}, ${r.points} points`;
    }
    case "not_counted":
      return "Not on our list";
    case "checking":
      return "We'll check this one";
    case "too_late":
      return "Too late";
    case "no_answer":
      return "No answer";
  }
}
