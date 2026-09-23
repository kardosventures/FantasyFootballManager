import { describe, expect, it } from "vitest";

import { isTerminalAction, operationalStatus } from "../lib/format";

describe("operator status formatting", () => {
  it("shows blockers without implying readiness", () => {
    expect(operationalStatus(false, 4)).toBe("4 blockers");
  });

  it("treats uncertain writes as terminal pending reconciliation", () => {
    expect(isTerminalAction("unverified")).toBe(true);
    expect(isTerminalAction("cancelled")).toBe(true);
    expect(isTerminalAction("superseded")).toBe(true);
    expect(isTerminalAction("ready")).toBe(false);
  });
});
