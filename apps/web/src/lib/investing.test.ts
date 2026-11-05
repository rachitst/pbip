import { describe, expect, it } from "vitest";
import { formatGainBps, formatUnits, parseUnits, unitsForAmount, valueOfUnits } from "./investing";

describe("investing units (ADR-0058)", () => {
  it("formats micro-units without floats or trailing zeros", () => {
    expect(formatUnits(12_345_678)).toBe("12.345678");
    expect(formatUnits(3_000_000)).toBe("3");
    expect(formatUnits(500_000)).toBe("0.5");
    expect(formatUnits(1)).toBe("0.000001");
  });

  it("parses units the customer typed", () => {
    expect(parseUnits("2", true)).toBe(2_000_000);
    expect(parseUnits("2.5", true)).toBeNull(); // stocks and bonds: whole units only
    expect(parseUnits("2.5", false)).toBe(2_500_000);
    expect(parseUnits("0.000001", false)).toBe(1);
    expect(parseUnits("0.0000001", false)).toBeNull();
    expect(parseUnits("0", false)).toBeNull();
    expect(parseUnits("-1", false)).toBeNull();
    expect(parseUnits("abc", false)).toBeNull();
  });

  it("rounds units down like the server", () => {
    expect(unitsForAmount(1_000_00, 300_00)).toBe(3_333_333);
    expect(valueOfUnits(3_333_333, 300_00)).toBe(99_999);
    expect(valueOfUnits(2_000_000, 845_50)).toBe(1_691_00);
  });

  it("formats gain with a sign", () => {
    expect(formatGainBps(123)).toBe("+1.23%");
    expect(formatGainBps(-50)).toBe("-0.50%");
    expect(formatGainBps(0)).toBe("0.00%");
  });
});
