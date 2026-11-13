import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { germanTranslit, normalize } from "./normalize";

interface Case { input: string; norm: string; translit: string | null }
const { cases } = JSON.parse(readFileSync("pipeline/tests/normalize_cases.json", "utf8")) as { cases: Case[] };

describe("normalize (shared cases with Python)", () => {
  for (const c of cases) {
    it(`${JSON.stringify(c.input)} -> ${JSON.stringify(c.norm)}`, () => {
      expect(normalize(c.input)).toBe(c.norm);
      expect(germanTranslit(c.input)).toBe(c.translit);
    });
  }
});
