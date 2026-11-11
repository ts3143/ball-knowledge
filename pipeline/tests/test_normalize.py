import json
from pathlib import Path

import pytest

from ingest.normalize import german_translit, normalize

CASES = json.loads((Path(__file__).parent / "normalize_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["input"] for c in CASES])
def test_normalize(case):
    assert normalize(case["input"]) == case["norm"]


@pytest.mark.parametrize("case", CASES, ids=[c["input"] for c in CASES])
def test_german_translit(case):
    assert german_translit(case["input"]) == case["translit"]


def test_umlaut_spellings_meet():
    # Müller, Mueller and Muller must all reach a shared alias.
    forms = {normalize("Müller"), german_translit("Müller")}
    assert normalize("Mueller") in forms
    assert normalize("Muller") in forms
