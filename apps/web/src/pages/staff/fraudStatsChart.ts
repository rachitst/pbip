import type { FraudStats } from "../../api/client";

/** The four decisions in stack order (bottom to top), with the chart colour of each.
 * Colour pairs were checked under simulated deuteranopia, protanopia and tritanopia: the one pair
 * that is hard to tell apart (Allowed, Held for review) is never adjacent in the stack, and every
 * chart also has a text summary and a data table, so no value is read from colour alone. */
export const DECISION_SERIES = [
  { key: "ALLOW", label: "Allowed", color: "var(--chart-1)", swatch: 1 },
  { key: "STEP_UP", label: "Extra verification", color: "var(--chart-3)", swatch: 3 },
  { key: "REVIEW", label: "Held for review", color: "var(--chart-4)", swatch: 4 },
  { key: "BLOCK", label: "Blocked", color: "var(--chart-5)", swatch: 5 },
] as const;

export type DayRow = FraudStats["by_day"][number] & {
  scored: number;
  /** Held and blocked shares of the day's decisions, in percent (null on a day with none). */
  holdPct: number | null;
  blockPct: number | null;
};

const DAY_MS = 86_400_000;

function utcDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

function pct(part: number, whole: number): number | null {
  return whole === 0 ? null : Math.round((part / whole) * 1000) / 10;
}

/** Every UTC day of the window, oldest first; a day without decisions is a row of zeros, so the
 * chart's time axis has no gaps that would hide a quiet day. */
export function dailyRows(stats: FraudStats): DayRow[] {
  const end = Date.parse(stats.generated_at);
  const byDay = new Map(stats.by_day.map((d) => [d.day, d]));
  const rows: DayRow[] = [];
  for (let ms = end - stats.window_days * DAY_MS; utcDay(ms) <= utcDay(end); ms += DAY_MS) {
    const day = utcDay(ms);
    const counts = byDay.get(day) ?? { day, ALLOW: 0, STEP_UP: 0, REVIEW: 0, BLOCK: 0 };
    const scored = counts.ALLOW + counts.STEP_UP + counts.REVIEW + counts.BLOCK;
    rows.push({
      ...counts,
      scored,
      holdPct: pct(counts.REVIEW, scored),
      blockPct: pct(counts.BLOCK, scored),
    });
  }
  return rows;
}

/** 0.0125 -> "1.3%" (the API returns shares between 0 and 1). */
export function formatRate(share: number): string {
  return `${(share * 100).toFixed(1)}%`;
}

/** "NEW_PAYEE_LARGE_AMOUNT" -> "New payee large amount": readable without a code table. */
export function reasonLabel(code: string): string {
  const words = code.toLowerCase().replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function bandLabel(band: { low: number; high: number }): string {
  return `${String(band.low)}-${String(band.high)}`;
}
