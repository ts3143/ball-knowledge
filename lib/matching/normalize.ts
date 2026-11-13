// Port of pipeline/ingest/normalize.py. pipeline/tests/normalize_cases.json holds the shared cases.

const SPECIAL: Record<string, string> = {
  "ø": "o", "æ": "ae", "œ": "oe", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i", "ħ": "h", "ŀ": "l",
};
const SPECIAL_RE = /[øæœłđðþıħŀ]/g;
const APOSTROPHES = /['’‘`´ʼ]/g;
const NON_WORD = /[^\p{L}\p{N}]+/gu;
const UMLAUTS: Record<string, string> = { "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue" };

/** Lowercase, strip accents, delete apostrophes, other punctuation to spaces. Müller -> muller, Strauß -> strauss. */
export function normalize(text: string): string {
  // ß (and ẞ, which lowercases to ß) is mapped explicitly; the rest of the special letters do not decompose in NFKD.
  let s = text.toLowerCase().replaceAll("ß", "ss").replace(SPECIAL_RE, (c) => SPECIAL[c] ?? c);
  s = s.normalize("NFKD").replace(/\p{M}/gu, "");
  s = s.replace(APOSTROPHES, "").replace(NON_WORD, " ");
  return s.trim().split(/\s+/).filter(Boolean).join(" ");
}

/** The ae/oe/ue spelling of a name with umlauts (Müller -> mueller), normalised. null when it has none. */
export function germanTranslit(text: string): string | null {
  const composed = text.normalize("NFC");
  if (!/[äöüÄÖÜ]/.test(composed)) return null;
  return normalize(composed.replace(/[äöüÄÖÜ]/g, (c) => UMLAUTS[c] ?? c));
}
