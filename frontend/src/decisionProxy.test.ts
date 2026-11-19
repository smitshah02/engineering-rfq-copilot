import { describe, expect, it } from "vitest";
import { DECISION_PROXY, ORDINARY_PROXY, decisionTarget } from "../decisionProxy";

describe("isolated development decision proxy", () => {
  it.each(["/api/v1/quote-decisions", "/api/v1/quote-decisions?x=1", "/api/v1/quote-decisions/Q-1/approval", "/api/v1/quote-decisions/Q-1/release"])("ordinary routing cannot capture %s", (path) => {
    expect(new RegExp(DECISION_PROXY).test(path)).toBe(true);
    expect(new RegExp(ORDINARY_PROXY).test(path)).toBe(false);
  });
  it("preserves ordinary routing", () => {
    expect(new RegExp(ORDINARY_PROXY).test("/api/v1/staff/quotes/Q-1/submit-for-review")).toBe(true);
    expect(new RegExp(DECISION_PROXY).test("/api/v1/staff/quotes")).toBe(false);
  });
  it.each(["https://external.invalid", "http://external.invalid:8001", "http://u:p@127.0.0.1:8001", "http://127.0.0.1:8001/path", "http://127.0.0.1:8001?x=1"])("refuses unsafe decision target %s", (target) => {
    expect(() => decisionTarget(target)).toThrow();
  });
  it("accepts an explicit loopback decision origin", () => {
    expect(decisionTarget("http://127.0.0.1:8001")).toBe("http://127.0.0.1:8001");
  });
});
