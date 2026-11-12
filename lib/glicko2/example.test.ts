import { describe, expect, it } from "vitest";
import { exampleLines } from "./example";
import { PYTHON_EXAMPLE_OUTPUT } from "./example.expected";

describe("worked example", () => {
  it("reproduces `python -m rating example` line for line", () => {
    const lines = exampleLines();
    console.log(lines.join("\n"));
    expect(lines.join("\n") + "\n").toBe(PYTHON_EXAMPLE_OUTPUT);
  });
});
