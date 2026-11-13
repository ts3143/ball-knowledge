import { describe, expect, it } from "vitest";
import { matchingConfig } from "@/lib/config";
import { buildIndex, matchAnswer, osaDistance, type AliasRow, type AnswerEntity, type OutsideEntity, type OutsideLookup } from "./index";
import { germanTranslit, normalize } from "./normalize";

const cfg = matchingConfig();
let nextId = 1;

function al(alias: string, kind: string, stale = false): AliasRow {
  // Like the ingest, a translit alias keeps the original text and stores the ae/oe/ue form as alias_norm.
  const alias_norm = kind === "translit" ? (germanTranslit(alias) as string) : normalize(alias);
  return { alias, alias_norm, kind, stale };
}

/** A synthetic person: label, family_name and the translit siblings the ingest would create. */
function person(
  name: string, family: string, share: number, tier: number, extra: AliasRow[] = [], id = nextId++,
): AnswerEntity {
  const aliases = [al(name, "label"), al(family, "family_name"), ...extra];
  return { entity_id: id, type: "person", canonical_name: name, predicted_share: share, tier, points: tier * 10, aliases };
}

function club(name: string, share: number, tier: number, extra: AliasRow[] = [], id = nextId++): AnswerEntity {
  return { entity_id: id, type: "club", canonical_name: name, predicted_share: share, tier, points: tier * 10, aliases: [al(name, "label"), ...extra] };
}

const noOutside = () => [] as OutsideEntity[];
const run = (answers: AnswerEntity[], input: string, outside: OutsideLookup = noOutside) =>
  matchAnswer(input, buildIndex(answers, cfg), outside, cfg);

describe("accents and transliteration", () => {
  const set = [
    person("Jonas Müllertest", "Müllertest", 0.1, 2, [al("Müllertest", "translit"), al("Jonas Müllertest", "translit")]),
  ];
  it.each(["Müllertest", "Muellertest", "Mullertest", "MÜLLERTEST", "jonas muellertest"])("%s matches", (input) => {
    const r = run(set, input);
    expect(r.verdict).toBe("matched");
    expect(r.matched_entity_id).toBe(set[0]!.entity_id);
  });
  it("reports the kind and the frozen tier and points", () => {
    const r = run(set, "Jonas Müllertest");
    expect(r).toMatchObject({ match_kind: "full", edit_distance: 0, tier: 2, points: 20, ambiguous: false });
    expect(run(set, "muellertest").match_kind).toBe("surname");
  });
});

describe("shared surnames", () => {
  const big = person("Anna Testmann", "Testmann", 0.3, 1, [], 50);
  const small = person("Bernd Testmann", "Testmann", 0.02, 4, [], 40);
  it("credits the most obvious Testmann and flags ambiguity", () => {
    const r = run([small, big], "Testmann");
    expect(r).toMatchObject({ verdict: "matched", matched_entity_id: 50, ambiguous: true, match_kind: "surname", tier: 1 });
    expect(r.candidates.map((c) => c.entity_id)).toEqual([50, 40]);
  });
  it("a full name is not ambiguous", () => {
    expect(run([small, big], "Bernd Testmann")).toMatchObject({ matched_entity_id: 40, ambiguous: false, match_kind: "full" });
  });
  it("equal shares go to the lowest entity id", () => {
    const a = person("A Testmann", "Testmann", 0.1, 1, [], 9);
    const b = person("B Testmann", "Testmann", 0.1, 1, [], 3);
    expect(run([a, b], "testmann").matched_entity_id).toBe(3);
  });
  it("a derived last token credits when there is no family_name alias", () => {
    const e: AnswerEntity = { ...person("Carl Dritt Beispiel", "x", 0.1, 3), aliases: [al("Carl Dritt Beispiel", "label")] };
    expect(run([e], "beispiel").match_kind).toBe("surname");
    expect(run([e], "dritt beispiel").match_kind).toBe("surname");
    expect(run([e], "dritt").verdict).toBe("wrong");
  });
});

describe("typos", () => {
  const set = [person("Franz Beispielmann", "Beispielmann", 0.1, 3), person("Karl Kurz", "Kurz", 0.1, 3)];
  it("one typo in a long surname", () => {
    expect(run(set, "beispielmahn")).toMatchObject({ verdict: "matched", match_kind: "fuzzy", edit_distance: 1 });
  });
  it("a transposition is one edit", () => {
    expect(osaDistance("beispielmann", "beispielmnan")).toBe(1);
    expect(run(set, "beispielmnan")).toMatchObject({ verdict: "matched", match_kind: "fuzzy", edit_distance: 1 });
  });
  it("two typos in a 12+ character key are allowed, three are not", () => {
    expect(run(set, "bexspielmahn").edit_distance).toBe(2);
    expect(run(set, "bexspielmahx").verdict).toBe("wrong");
  });
  it("a 4-character name never fuzzy-matches", () => {
    expect(run(set, "kurs")).toMatchObject({ verdict: "wrong" });
    expect(run(set, "kurz").verdict).toBe("matched");
  });
  it("a short 5-8 character key allows one edit only", () => {
    const s = [person("Otto Brandt", "Brandt", 0.1, 2)];
    expect(run(s, "brandd").verdict).toBe("matched");
    expect(run(s, "brxndd").verdict).toBe("wrong");
  });
  it("fuzzy ties go to the smaller distance, then the more obvious entity", () => {
    const a = person("Lena Hartwig", "Hartwig", 0.05, 4, [], 2);
    const b = person("Lena Hartwik", "Hartwik", 0.2, 1, [], 1);
    expect(run([a, b], "hartwig").matched_entity_id).toBe(2); // exact beats fuzzy
    const r = run([a, b], "hartwit"); // both at distance 1, same class: higher share wins
    expect(r).toMatchObject({ matched_entity_id: 1, ambiguous: true });
  });
});

describe("nicknames and clubs", () => {
  it("a nickname is a full key", () => {
    const e = person("Hans Beispiel", "Beispiel", 0.1, 2, [al("Hansi Kanone", "nickname")]);
    expect(run([e], "hansi kanone")).toMatchObject({ verdict: "matched", match_kind: "full" });
  });
  const answers = [
    club("1. FC Testhausen 05", 0.2, 2, [al("Testhausen", "short_name")], 1),
    club("FC Beispielstadt", 0.1, 3, [], 2),
  ];
  it("exact and partial club names", () => {
    expect(run(answers, "1. FC Testhausen 05").match_kind).toBe("full");
    expect(run(answers, "Testhausen")).toMatchObject({ matched_entity_id: 1, match_kind: "full" });
    expect(run(answers, "FC Testhausen")).toMatchObject({ matched_entity_id: 1, match_kind: "partial" });
  });
  it("too-short and all-digit n-grams are not keys", () => {
    expect(run(answers, "FC").verdict).toBe("wrong");
    expect(run(answers, "05").verdict).toBe("wrong");
    expect(run(answers, "1").verdict).toBe("wrong");
  });
  it("an ambiguous partial credits the most obvious club", () => {
    const r = run(answers, "fc");
    expect(r.verdict).toBe("wrong");
    const two = [club("Verein Alpha", 0.3, 1, [], 7), club("Verein Beta", 0.05, 4, [], 8)];
    expect(run(two, "verein")).toMatchObject({ matched_entity_id: 7, ambiguous: true, match_kind: "partial" });
  });
});

describe("apostrophes", () => {
  const set = [person("Kevin O'Testmann", "O'Testmann", 0.1, 2)];
  it.each(["O'Testmann", "O’Testmann", "otestmann", "Kevin O`Testmann"])("%s matches", (input) => {
    expect(run(set, input).verdict).toBe("matched");
  });
});

describe("outside real entities", () => {
  const set = [person("Jonas Fischertest", "Fischertest", 0.1, 3)];
  const outside = (n: string): OutsideEntity[] => (n === "fischertesr" ? [{ entity_id: 999, canonical_name: "Fischertesr" }] : []);
  it("control: without the lookup the same typo is credited", () => {
    // control: without the lookup, the typo is credited fuzzily
    expect(run(set, "fischertesr")).toMatchObject({ verdict: "matched", match_kind: "fuzzy" });
  });
  it("is unmatched_entity, not fuzzy credit", () => {
    const r = run(set, "Fischertesr", outside);
    expect(r).toMatchObject({ verdict: "unmatched_entity", points: 0, matched_entity_id: null });
    expect(r.outside).toEqual([{ entity_id: 999, canonical_name: "Fischertesr" }]);
  });
  it("an exact match wins over the lookup", () => {
    expect(run(set, "fischertest", () => [{ entity_id: 999, canonical_name: "X" }]).verdict).toBe("matched");
  });
  it("ignores lookup hits that are in the answer set; caps candidates", () => {
    const many = Array.from({ length: 30 }, (_, i) => ({ entity_id: 1000 + i, canonical_name: `E${i}` }));
    expect(run(set, "unknownname", () => many).outside).toHaveLength(cfg.review_candidates_max);
    expect(run(set, "unknownname", () => [{ entity_id: set[0]!.entity_id, canonical_name: "x" }]).verdict).toBe("wrong");
  });
});

describe("blank, wrong and truncation", () => {
  const set = [person("Jonas Fischertest", "Fischertest", 0.1, 3)];
  it("blank and whitespace-only input", () => {
    for (const input of ["", "   ", "\n\t", "...", "'"]) expect(run(set, input)).toMatchObject({ verdict: "blank", points: 0 });
  });
  it("unrelated input is wrong with 0 points", () => {
    expect(run(set, "zzzz")).toMatchObject({ verdict: "wrong", points: 0, tier: null });
  });
  it("input longer than max_input_chars is truncated", () => {
    const long = "fischertest " + "x".repeat(cfg.max_input_chars + 50);
    const r = run(set, long);
    expect(r.truncated).toBe(true);
    expect(r.input_norm.length).toBeLessThanOrEqual(cfg.max_input_chars);
    // The part after the cut never leaks into the match: a name placed beyond the limit does not count.
    const beyond = "y".repeat(cfg.max_input_chars) + " fischertest";
    expect(run(set, beyond)).toMatchObject({ verdict: "wrong", truncated: true });
  });
  it("stale aliases are not keys", () => {
    const e = person("Jonas Fischertest", "Fischertest", 0.1, 3, [al("Altname", "alt_label", true)]);
    expect(run([e], "altname").verdict).toBe("wrong");
  });
});
