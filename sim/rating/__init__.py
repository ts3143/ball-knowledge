"""The Glicko-2 reference engine. TypeScript ports it (lib/glicko2) and must reproduce tests/glicko2_cases.json."""

# Bump on any change in meaning (formulas, period rules, scope rules); then regenerate the golden file.
ENGINE_VERSION = "bk-glicko-2"
