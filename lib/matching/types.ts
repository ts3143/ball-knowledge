export const MATCHER_VERSION = "bk-match-1";

export type EntityType = "person" | "club";
export type KeyClass = "full" | "surname" | "partial";
export type MatchKind = KeyClass | "fuzzy";
export type Verdict = "matched" | "wrong" | "unmatched_entity" | "blank";

/** An entity_aliases row. */
export interface AliasRow {
  alias: string;
  alias_norm: string;
  kind: string; // label | alt_label | short_name | nickname | family_name | translit
  stale?: boolean;
}

/** One row of the prompt's answer set (prompt_answers joined to its entity and aliases). */
export interface AnswerEntity {
  entity_id: number;
  type: EntityType;
  canonical_name: string;
  predicted_share: number;
  tier: number;
  points: number;
  hipster_pick?: boolean;
  aliases: AliasRow[];
}

/** A real entity outside the answer set whose non-stale alias exactly equals the input. */
export interface OutsideEntity {
  entity_id: number;
  canonical_name: string;
}

/** Given a normalised input, the other entities of the prompt's answer type with that exact alias. */
export type OutsideLookup = (inputNorm: string) => OutsideEntity[];

export interface MatchConfig {
  fuzzy_one_edit_min_chars: number;
  fuzzy_two_edits_min_chars: number;
  partial_min_chars: number;
  derived_surname_min_chars: number;
  max_input_chars: number;
  review_candidates_max: number;
}

export interface Candidate {
  entity_id: number;
  name: string;
  key: string;
  key_class: KeyClass;
  distance: number;
  predicted_share: number;
}

export interface MatchResult {
  matcher_version: string;
  verdict: Verdict;
  input_norm: string;
  truncated: boolean;
  match_kind: MatchKind | null;
  edit_distance: number | null;
  ambiguous: boolean;
  matched_entity_id: number | null;
  matched_name: string | null;
  matched_key: string | null;
  tier: number | null;
  points: number;
  hipster_pick: boolean;
  /** The entities considered for the verdict (best first), for debugging and the review queue. */
  candidates: Candidate[];
  /** For unmatched_entity: real entities outside the set, at most review_candidates_max. */
  outside: OutsideEntity[];
}
