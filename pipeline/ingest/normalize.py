"""Name normalization for aliases. Step 5's TypeScript matcher must produce the same output:
tests/normalize_cases.json holds the shared cases."""

import re
import unicodedata

# Letters that don't decompose into base letter + accent under NFKD.
_SPECIAL = str.maketrans({
    "ø": "o", "æ": "ae", "œ": "oe", "ł": "l", "đ": "d", "ð": "d",
    "þ": "th", "ı": "i", "ħ": "h", "ŀ": "l",
})
_APOSTROPHES = re.compile(r"['’‘`´ʼ]")
_NON_WORD = re.compile(r"[\W_]+")
_GERMAN_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})


def normalize(text: str) -> str:
    """Lowercase, strip accents, delete apostrophes, other punctuation to spaces.

    Müller -> muller, Strauß -> strauss, "1. FC Köln" -> "1 fc koln", N'Diaye -> ndiaye.
    """
    s = text.casefold().translate(_SPECIAL)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _APOSTROPHES.sub("", s)
    s = _NON_WORD.sub(" ", s)
    return " ".join(s.split())


def german_translit(text: str) -> str | None:
    """The ae/oe/ue spelling of a name with umlauts (Müller -> mueller), normalized.
    None when the name has no umlauts."""
    composed = unicodedata.normalize("NFC", text)
    if not any(c in composed for c in "äöüÄÖÜ"):
        return None
    return normalize(composed.translate(_GERMAN_UMLAUTS))
