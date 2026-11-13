// Pure timer rules for a dive. The server clock decides; the browser only displays a countdown.
//
//   served_at ... served_at + seconds_per_prompt           the prompt is open (remaining_ms counts down to 0)
//   ... + grace_seconds                                     an answer still counts (network lag)
//   after that                                              an answer is `late` (stored, not matched, 0 points)
//
// A dive closes at closes_at; an attempt started before that may keep going until closes_at + late_finish_minutes,
// after which it is finalised as abandoned.
import { loadTunables, type Tunables } from "@/lib/config";

export interface TimerConfig {
  seconds_per_prompt: number;
  grace_seconds: number;
  late_finish_minutes: number;
}

/** The [dive] timer tunables. loadTunables() re-reads tunables.toml when its mtime changes outside production. */
export function timerConfig(tunables: Tunables = loadTunables()): TimerConfig {
  const d = tunables.dive;
  return {
    seconds_per_prompt: Number(d.seconds_per_prompt),
    grace_seconds: Number(d.grace_seconds),
    late_finish_minutes: Number(d.late_finish_minutes),
  };
}

/** When the prompt's timer runs out (what the player's countdown shows as 0). */
export function deadlineAt(servedAt: Date, cfg: TimerConfig): Date {
  return new Date(servedAt.getTime() + cfg.seconds_per_prompt * 1000);
}

/** The last instant an answer still counts: the deadline plus the grace. */
export function graceEndAt(servedAt: Date, cfg: TimerConfig): Date {
  return new Date(deadlineAt(servedAt, cfg).getTime() + cfg.grace_seconds * 1000);
}

/** Milliseconds left on the player's countdown; never negative. */
export function remainingMs(servedAt: Date, now: Date, cfg: TimerConfig): number {
  return Math.max(0, deadlineAt(servedAt, cfg).getTime() - now.getTime());
}

/** True once deadline + grace has passed: an answer now is `late`, an unanswered prompt is `timeout`. */
export function isLate(servedAt: Date, now: Date, cfg: TimerConfig): boolean {
  return now.getTime() > graceEndAt(servedAt, cfg).getTime();
}

/** Time from serving to answering, in whole milliseconds (never negative). */
export function responseMs(servedAt: Date, answeredAt: Date): number {
  return Math.max(0, Math.round(answeredAt.getTime() - servedAt.getTime()));
}

/** A dive can be started while opens_at <= now < closes_at. */
export function diveIsOpen(opensAt: Date, closesAt: Date, now: Date): boolean {
  return opensAt.getTime() <= now.getTime() && now.getTime() < closesAt.getTime();
}

/** The last instant an attempt on a dive that closes at `closesAt` can still be played. */
export function attemptWindowEnd(closesAt: Date, cfg: TimerConfig): Date {
  return new Date(closesAt.getTime() + cfg.late_finish_minutes * 60_000);
}

/** False once the late-finish window has passed: the attempt is finalised as abandoned. */
export function attemptWindowOpen(closesAt: Date, now: Date, cfg: TimerConfig): boolean {
  return now.getTime() <= attemptWindowEnd(closesAt, cfg).getTime();
}
