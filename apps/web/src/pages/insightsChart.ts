import type { CSSProperties } from "react";
import type { CategorySpendingItem } from "../api/client";
import { formatMinor } from "../lib/money";

/** Recharts draws its default tooltip with a fixed white background and the series colour as
 * text, which is unreadable in the dark theme. These styles use the theme tokens instead. */
export const TOOLTIP_STYLE: {
  contentStyle: CSSProperties;
  itemStyle: CSSProperties;
  labelStyle: CSSProperties;
} = {
  contentStyle: {
    background: "var(--surface)",
    border: "1px solid var(--border-strong)",
    borderRadius: "var(--radius)",
    color: "var(--text)",
  },
  itemStyle: { color: "var(--text)" },
  labelStyle: { color: "var(--muted)" },
};

export function formatShare(percentage: number): string {
  return `${percentage.toFixed(1)}%`;
}

/** Tooltip text for a category slice: the amount and its share of the month's spending. */
export function categoryTooltip(
  categories: readonly CategorySpendingItem[],
  currency: string,
): (value: unknown, name: unknown) => [string, string] {
  return (value, name) => {
    const category = String(name);
    const amount = formatMinor(Number(value), currency);
    const share = categories.find((item) => item.category === category)?.percentage;
    return [share === undefined ? amount : `${amount} (${formatShare(share)})`, category];
  };
}
