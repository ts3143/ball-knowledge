import { normalize } from "./normalize";
import {
  MATCHER_VERSION,
  type AnswerEntity,
  type Candidate,
  type KeyClass,
  type MatchConfig,
  type MatchResult,
  type OutsideLookup,
} from "./types";

export * from "./types";
export { normalize, germanTranslit } from "./normalize";

const CLASS_RANK: Record<KeyClass, number> = { full: 0, surname: 1, partial: 2 };

interface KeyEntry {
  key: string;
  key_class: KeyClass;
  entity: AnswerEntity;
}

export interface AnswerIndex {
  /** key -> entries per class; a key can belong to several entities (ambiguity). */
  exact: Record<KeyClass, Map<string, AnswerEntity[]>>;
  entries: KeyEntry[];
  ids: Set<number>;
}

const allDigits = (s: string) => /^\d+$/.test(s.replaceAll(" ", ""));

/** Keys of one answer, by class. */
export function keysFor(a: AnswerEntity, cfg: MatchConfig): Record<KeyClass, Set<string>> {
  const full = new Set<string>();
  const surname = new Set<string>();
  const partial = new Set<string>();
  const live = a.aliases.filter((x) => !x.stale && x.alias_norm !== "");
  const canonical = normalize(a.canonical_name);
  if (canonical) full.add(canonical);

  if (a.type === "person") {
    const familyTexts = new Set(live.filter((x) => x.kind === "family_name").map((x) => x.alias));
    for (const x of live) {
      if (x.kind === "family_name" || (x.kind === "translit" && familyTexts.has(x.alias))) surname.add(x.alias_norm);
      else full.add(x.alias_norm);
    }
    // Derived surname forms: the last token, and the last two when the label has 3+ tokens.
    const labels = [canonical, ...live.filter((x) => ["label", "alt_label", "translit"].includes(x.kind)).map((x) => x.alias_norm)];
    for (const label of labels) {
      const tokens = label.split(" ").filter(Boolean);
      if (tokens.length < 2) continue;
      const last = tokens[tokens.length - 1] as string;
      if (last.length >= cfg.derived_surname_min_chars) surname.add(last);
      if (tokens.length >= 3) surname.add(tokens.slice(-2).join(" "));
    }
  } else {
    for (const x of live) full.add(x.alias_norm);
    for (const label of [canonical, ...live.map((x) => x.alias_norm)]) {
      const tokens = label.split(" ").filter(Boolean);
      for (let i = 0; i < tokens.length; i++) {
        for (let j = i + 1; j <= tokens.length; j++) {
          const gram = tokens.slice(i, j).join(" ");
          if (gram.length >= cfg.partial_min_chars && !allDigits(gram)) partial.add(gram);
        }
      }
    }
  }
  // A key keeps only its strongest class.
  for (const k of full) { surname.delete(k); partial.delete(k); }
  for (const k of surname) partial.delete(k);
  return { full, surname, partial };
}

export function buildIndex(answers: AnswerEntity[], cfg: MatchConfig): AnswerIndex {
  const exact: AnswerIndex["exact"] = { full: new Map(), surname: new Map(), partial: new Map() };
  const entries: KeyEntry[] = [];
  for (const a of answers) {
    const keys = keysFor(a, cfg);
    for (const cls of ["full", "surname", "partial"] as const) {
      for (const key of keys[cls]) {
        const list = exact[cls].get(key) ?? [];
        list.push(a);
        exact[cls].set(key, list);
        entries.push({ key, key_class: cls, entity: a });
      }
    }
  }
  return { exact, entries, ids: new Set(answers.map((a) => a.entity_id)) };
}

/** Optimal string alignment distance (Levenshtein plus adjacent transposition). */
export function osaDistance(a: string, b: string): number {
  const n = a.length, m = b.length;
  if (n === 0) return m;
  if (m === 0) return n;
  const d: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = 0; i <= n; i++) (d[i] as number[])[0] = i;
  for (let j = 0; j <= m; j++) (d[0] as number[])[j] = j;
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      const row = d[i] as number[];
      let v = Math.min((d[i - 1] as number[])[j]! + 1, row[j - 1]! + 1, (d[i - 1] as number[])[j - 1]! + cost);
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        v = Math.min(v, (d[i - 2] as number[])[j - 2]! + 1);
      }
      row[j] = v;
    }
  }
  return (d[n] as number[])[m] as number;
}

/** Edits allowed for a key of this length. */
export function allowedEdits(keyLength: number, cfg: MatchConfig): number {
  if (keyLength >= cfg.fuzzy_two_edits_min_chars) return 2;
  if (keyLength >= cfg.fuzzy_one_edit_min_chars) return 1;
  return 0;
}

/** Most obvious first: highest predicted share, then lowest entity id. */
const byObviousness = (a: AnswerEntity, b: AnswerEntity) =>
  b.predicted_share - a.predicted_share || a.entity_id - b.entity_id;

function candidate(a: AnswerEntity, key: string, cls: KeyClass, distance: number): Candidate {
  return { entity_id: a.entity_id, name: a.canonical_name, key, key_class: cls, distance, predicted_share: a.predicted_share };
}

function base(inputNorm: string, truncated: boolean): MatchResult {
  return {
    matcher_version: MATCHER_VERSION, verdict: "wrong", input_norm: inputNorm, truncated, match_kind: null,
    edit_distance: null, ambiguous: false, matched_entity_id: null, matched_name: null, matched_key: null,
    tier: null, points: 0, hipster_pick: false, candidates: [], outside: [],
  };
}

function credit(r: MatchResult, a: AnswerEntity, kind: MatchResult["match_kind"], distance: number, key: string): MatchResult {
  return {
    ...r, verdict: "matched", match_kind: kind, edit_distance: distance, matched_entity_id: a.entity_id,
    matched_name: a.canonical_name, matched_key: key, tier: a.tier, points: a.points, hipster_pick: a.hipster_pick ?? false,
  };
}

/**
 * Matches typed input against a prompt's answer set. Order: blank; exact by class (full > surname > partial, an
 * ambiguous key credits the most obvious entity); a real entity outside the set -> unmatched_entity (no fuzzy
 * credit); fuzzy; wrong.
 */
export function matchAnswer(
  raw: string,
  index: AnswerIndex,
  outside: OutsideLookup,
  cfg: MatchConfig,
): MatchResult {
  const chars = Array.from(raw ?? "");
  const truncated = chars.length > cfg.max_input_chars;
  const input = normalize(truncated ? chars.slice(0, cfg.max_input_chars).join("") : chars.join(""));
  const r = base(input, truncated);
  if (input === "") return { ...r, verdict: "blank" };

  for (const cls of ["full", "surname", "partial"] as const) {
    const hits = index.exact[cls].get(input);
    if (hits && hits.length > 0) {
      const sorted = [...hits].sort(byObviousness);
      const out = credit(r, sorted[0] as AnswerEntity, cls, 0, input);
      out.ambiguous = sorted.length > 1;
      out.candidates = sorted.map((a) => candidate(a, input, cls, 0));
      return out;
    }
  }

  const others = outside(input).filter((o) => !index.ids.has(o.entity_id));
  if (others.length > 0) {
    return { ...r, verdict: "unmatched_entity", outside: others.slice(0, cfg.review_candidates_max) };
  }

  const found: Candidate[] = [];
  const entityOf = new Map<number, AnswerEntity>();
  for (const e of index.entries) {
    const allowed = allowedEdits(e.key.length, cfg);
    if (allowed === 0 || Math.abs(e.key.length - input.length) > allowed) continue;
    const dist = osaDistance(input, e.key);
    if (dist <= allowed) {
      found.push(candidate(e.entity, e.key, e.key_class, dist));
      entityOf.set(e.entity.entity_id, e.entity);
    }
  }
  if (found.length === 0) return r;
  found.sort((a, b) =>
    a.distance - b.distance || CLASS_RANK[a.key_class] - CLASS_RANK[b.key_class] ||
    b.predicted_share - a.predicted_share || a.entity_id - b.entity_id);
  const best = found[0] as Candidate;
  const tied = new Set(found.filter((c) => c.distance === best.distance && c.key_class === best.key_class).map((c) => c.entity_id));
  const out = credit(r, entityOf.get(best.entity_id) as AnswerEntity, "fuzzy", best.distance, best.key);
  out.ambiguous = tied.size > 1;
  out.candidates = found;
  return out;
}
