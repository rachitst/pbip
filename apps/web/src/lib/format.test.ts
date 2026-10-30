import { describe, expect, it } from "vitest";
import { formatDate, formatDateTime } from "./format";

describe("date formatting", () => {
  it("formats dates and date-times in the bank's locale", () => {
    expect(formatDate("2026-01-15T00:00:00Z")).toMatch(/2026/);
    expect(formatDateTime("2026-10-01T10:00:00Z")).toMatch(/2026/);
    expect(formatDateTime("2026-10-01T10:00:00Z")).not.toBe(formatDate("2026-10-01T10:00:00Z"));
  });
});
