// Pure scoring. The effective answer for a position is the latest row by id (a review correction is appended).

export type AnswerVerdict = "matched" | "wrong" | "unmatched_entity" | "blank" | "late" | "timeout";

export interface AnswerRow {
  id: number;
  position: number;
  verdict: AnswerVerdict;
  raw_text: string | null;
  matched_entity_id: number | null;
  tier: number | null;
  hipster_pick: boolean;
  points: number;
}

export interface PromptRef {
  position: number;
  prompt_id: number;
}

export interface PromptResult {
  position: number;
  prompt_id: number;
  /** null when the position has no effective row (never served or never answered): scores 0. */
  answered: boolean;
  verdict: AnswerVerdict | "unanswered";
  raw_text: string | null;
  matched_entity_id: number | null;
  tier: number | null;
  hipster_pick: boolean;
  points: number;
}

export interface Score {
  results: PromptResult[];
  total: number;
  max: number;
}

/** The latest row (highest id) per position. */
export function effectiveAnswers(rows: readonly AnswerRow[]): Map<number, AnswerRow> {
  const out = new Map<number, AnswerRow>();
  for (const r of rows) {
    const cur = out.get(r.position);
    if (!cur || r.id > cur.id) out.set(r.position, r);
  }
  return out;
}

/** `maxPoints` is max(tiers.points); the total is out of prompts.length * maxPoints. */
export function scoreAttempt(prompts: readonly PromptRef[], rows: readonly AnswerRow[], maxPoints: number): Score {
  const effective = effectiveAnswers(rows);
  const ordered = [...prompts].sort((a, b) => a.position - b.position);
  const results = ordered.map<PromptResult>((p) => {
    const row = effective.get(p.position);
    if (!row) {
      return { position: p.position, prompt_id: p.prompt_id, answered: false, verdict: "unanswered", raw_text: null,
        matched_entity_id: null, tier: null, hipster_pick: false, points: 0 };
    }
    // Only a matched row can score, whatever the stored points say.
    const points = row.verdict === "matched" ? row.points : 0;
    return { position: p.position, prompt_id: p.prompt_id, answered: true, verdict: row.verdict, raw_text: row.raw_text,
      matched_entity_id: row.matched_entity_id, tier: row.tier, hipster_pick: row.hipster_pick, points };
  });
  return { results, total: results.reduce((s, r) => s + r.points, 0), max: ordered.length * maxPoints };
}
