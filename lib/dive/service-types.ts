// Response shapes of the dive API: what the player is allowed to see. Type-only, safe to import in the browser.
export type PlayerVerdict = "counted" | "not_counted" | "checking" | "too_late" | "no_answer";

export interface PlayerResult {
  position: number;
  verdict: PlayerVerdict;
  matched_name: string | null;
  tier: number | null;
  tier_name: string | null;
  hipster_pick: boolean;
  points: number;
}

export interface AttemptInfo {
  id: number;
  status: "in_progress" | "finished" | "abandoned";
  current_position: number;
  score: number | null;
}

export interface DiveInfo {
  id: number;
  dive_date: string;
  prompt_count: number;
  seconds_per_prompt: number;
  opens_at: string;
  closes_at: string;
  /** opens_at <= now < closes_at: a new attempt can start. False for a closed dive someone may still finish. */
  open: boolean;
}

export interface CurrentDive {
  state: "no_dive" | "closed" | "available";
  dive: DiveInfo | null;
  attempt: AttemptInfo | null;
}

export interface CurrentPrompt {
  position: number;
  prompt_count: number;
  text: string;
  remaining_ms: number;
  /** Set once this prompt is resolved (answered or timed out); the player is waiting to tap Next. */
  result: PlayerResult | null;
}

export interface SummaryLine extends PlayerResult {
  text: string;
  answer: string | null;
}

export interface Summary {
  attempt_id: number;
  status: "finished" | "abandoned";
  total: number;
  max: number;
  results: SummaryLine[];
}

export interface AttemptState {
  attempt_id: number;
  status: AttemptInfo["status"];
  prompt_count: number;
  current: CurrentPrompt | null;
  /** Resolved positions before the current one. */
  results: PlayerResult[];
  summary: Summary | null;
}

export type NextResult = Omit<CurrentPrompt, "result"> | { done: true };

export interface AnswerResponse extends PlayerResult {
  is_last: boolean;
}

