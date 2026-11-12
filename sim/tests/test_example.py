"""The plain-language example runs on fixed demo parameters and matches what the TypeScript port asserts."""

import re
from pathlib import Path

from rating.example import example

TS_EXPECTED = Path(__file__).resolve().parents[2] / "lib" / "glicko2" / "example.expected.ts"


def test_example_output_matches_the_typescript_expectation(capsys):
    example()
    out = capsys.readouterr().out
    expected = re.search(r"PYTHON_EXAMPLE_OUTPUT = `(.*?)`;", TS_EXPECTED.read_text(), re.DOTALL).group(1)
    assert out == expected
