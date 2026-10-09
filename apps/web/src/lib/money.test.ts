import { describe, expect, it } from "vitest";
import { formatMinor, parseToMinor } from "./money";

describe("formatMinor", () => {
  it("formats with Indian digit grouping and two decimals", () => {
    expect(formatMinor(123456789)).toBe("INR 12,34,567.89");
    expect(formatMinor(5)).toBe("INR 0.05");
    expect(formatMinor(-2500)).toBe("-INR 25.00");
  });
});

describe("parseToMinor", () => {
  it("parses valid amounts exactly (no float error)", () => {
    expect(parseToMinor("1,250.50")).toBe(125050);
    expect(parseToMinor("0.1")).toBe(10);
    expect(parseToMinor("19.99")).toBe(1999);
  });
  it.each(["", "abc", "-5", "1.234", "0", "0.00", "1e3"])("rejects %j", (value) => {
    expect(parseToMinor(value)).toBeNull();
  });
});
