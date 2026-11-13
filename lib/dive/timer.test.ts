import { mkdtempSync, readFileSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { loadTunables, TUNABLES_PATH } from "@/lib/config";
import {
  attemptWindowEnd, attemptWindowOpen, deadlineAt, diveIsOpen, graceEndAt, isLate, remainingMs, responseMs,
  timerConfig, type TimerConfig,
} from "./timer";

const cfg: TimerConfig = { seconds_per_prompt: 25, grace_seconds: 3, late_finish_minutes: 15 };
const served = new Date("2030-01-01T12:00:00.000Z");
const at = (ms: number) => new Date(served.getTime() + ms);

describe("prompt timer", () => {
  it("deadline and grace end come from served_at", () => {
    expect(deadlineAt(served, cfg)).toEqual(at(25_000));
    expect(graceEndAt(served, cfg)).toEqual(at(28_000));
  });

  it("remaining_ms falls with the clock and stops at 0", () => {
    expect(remainingMs(served, at(0), cfg)).toBe(25_000);
    expect(remainingMs(served, at(7_500), cfg)).toBe(17_500);
    expect(remainingMs(served, at(25_000), cfg)).toBe(0);
    expect(remainingMs(served, at(40_000), cfg)).toBe(0);
  });

  it("an answer is on time up to deadline + grace inclusive, late after", () => {
    expect(isLate(served, at(24_999), cfg)).toBe(false);
    expect(isLate(served, at(25_001), cfg)).toBe(false); // inside the grace
    expect(isLate(served, at(28_000), cfg)).toBe(false);
    expect(isLate(served, at(28_001), cfg)).toBe(true);
  });

  it("response_ms is never negative", () => {
    expect(responseMs(served, at(1_234))).toBe(1_234);
    expect(responseMs(served, at(-50))).toBe(0);
  });
});

describe("dive window", () => {
  const opens = new Date("2030-01-01T05:00:00Z");
  const closes = new Date("2030-01-02T05:00:00Z");

  it("is open from opens_at (inclusive) to closes_at (exclusive)", () => {
    expect(diveIsOpen(opens, closes, new Date(opens.getTime() - 1))).toBe(false);
    expect(diveIsOpen(opens, closes, opens)).toBe(true);
    expect(diveIsOpen(opens, closes, new Date(closes.getTime() - 1))).toBe(true);
    expect(diveIsOpen(opens, closes, closes)).toBe(false);
  });

  it("an attempt may continue until closes_at + late_finish_minutes", () => {
    expect(attemptWindowEnd(closes, cfg)).toEqual(new Date("2030-01-02T05:15:00Z"));
    expect(attemptWindowOpen(closes, new Date("2030-01-02T05:15:00Z"), cfg)).toBe(true);
    expect(attemptWindowOpen(closes, new Date("2030-01-02T05:15:00.001Z"), cfg)).toBe(false);
  });
});

describe("timer tunables", () => {
  it("are read from tunables.toml", () => {
    const real = timerConfig();
    expect(real.seconds_per_prompt).toBeGreaterThan(0);
    expect(real.grace_seconds).toBeGreaterThanOrEqual(0);
    expect(real.late_finish_minutes).toBeGreaterThan(0);
  });

  it("an edit to the file applies without a restart (mtime changes outside production)", () => {
    const dir = mkdtempSync(join(tmpdir(), "bk-tunables-"));
    const path = join(dir, "tunables.toml");
    const original = readFileSync(TUNABLES_PATH, "utf8");
    writeFileSync(path, original);
    utimesSync(path, new Date(1_000_000), new Date(1_000_000));
    const before = timerConfig(loadTunables(path)).seconds_per_prompt;
    expect(loadTunables(path)).toBe(loadTunables(path)); // unchanged file: cached copy

    writeFileSync(path, original.replace(/^seconds_per_prompt = \d+/m, `seconds_per_prompt = ${before + 7}`));
    utimesSync(path, new Date(2_000_000), new Date(2_000_000));
    expect(timerConfig(loadTunables(path)).seconds_per_prompt).toBe(before + 7);
  });
});
