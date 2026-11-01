import { describe, expect, it } from "vitest";
import { countOf, formatDate, formatDateTime, formatDuration, formatRelative } from "./format";

describe("date formatting", () => {
  it("formats dates and date-times in the bank's locale", () => {
    expect(formatDate("2026-01-15T00:00:00Z")).toMatch(/2026/);
    expect(formatDateTime("2026-10-01T10:00:00Z")).toMatch(/2026/);
    expect(formatDateTime("2026-10-01T10:00:00Z")).not.toBe(formatDate("2026-10-01T10:00:00Z"));
  });
});

describe("relative times and counts", () => {
  const now = Date.parse("2026-11-01T12:00:00Z");

  it("says how long ago, in the largest whole unit, with correct plurals", () => {
    expect(formatRelative("2026-11-01T11:59:30Z", now)).toBe("just now");
    expect(formatRelative("2026-11-01T12:00:05Z", now)).toBe("just now"); // clock skew
    expect(formatRelative("2026-11-01T11:59:00Z", now)).toBe("1 minute ago");
    expect(formatRelative("2026-11-01T11:15:00Z", now)).toBe("45 minutes ago");
    expect(formatRelative("2026-11-01T11:00:00Z", now)).toBe("1 hour ago");
    expect(formatRelative("2026-11-01T09:00:00Z", now)).toBe("3 hours ago");
    expect(formatRelative("2026-10-31T12:00:00Z", now)).toBe("yesterday");
    expect(formatRelative("2026-10-29T12:00:00Z", now)).toBe("3 days ago");
  });

  it("states durations and counts in words", () => {
    expect(formatDuration(30)).toBe("less than a minute");
    expect(formatDuration(60)).toBe("1 minute");
    expect(formatDuration(7_200)).toBe("2 hours");
    expect(formatDuration(86_400)).toBe("1 day");
    expect(countOf(0, "alert")).toBe("0 alerts");
    expect(countOf(1, "alert")).toBe("1 alert");
    expect(countOf(2, "alert")).toBe("2 alerts");
    expect(countOf(12_345, "event")).toBe("12,345 events");
  });
});
