import type { CSSProperties } from "react";
import type { CategorySpendingItem } from "../api/client";
import { categoryLabel } from "../lib/analytics";
import { formatMinor } from "../lib/money";

/** The categorical palette has five colours (tokens.css); more slices would repeat a colour. */
export const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
] as const;

export interface Slice {
  name: string;
  value: number;
  percentage: number;
  fill: string;
  /** 1-based palette position, for the matching legend swatch class. */
  colorIndex: number;
  /** API category keys drawn in this slice (several for "Other"). */
  categories: string[];
}

/** Donut slices: every category when they fit the palette, otherwise the four largest plus
 * "Other", so each slice has its own colour. The list and table still show every category. */
export function donutSlices(categories: readonly CategorySpendingItem[]): Slice[] {
  const sorted = [...categories].sort((a, b) => b.amount_minor - a.amount_minor);
  const slice = (items: CategorySpendingItem[], name: string, index: number): Slice => ({
    name,
    value: items.reduce((sum, item) => sum + item.amount_minor, 0),
    percentage: items.reduce((sum, item) => sum + item.percentage, 0),
    fill: CHART_COLORS[index] ?? "var(--chart-5)",
    colorIndex: index + 1,
    categories: items.map((item) => item.category),
  });
  if (sorted.length <= CHART_COLORS.length) {
    return sorted.map((item, index) => slice([item], categoryLabel(item.category), index));
  }
  const top = sorted.slice(0, CHART_COLORS.length - 1);
  return [
    ...top.map((item, index) => slice([item], categoryLabel(item.category), index)),
    slice(sorted.slice(CHART_COLORS.length - 1), "Other", CHART_COLORS.length - 1),
  ];
}

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

/** Tooltip text for a slice: the amount and its share of the month's spending. */
export function categoryTooltip(
  slices: readonly Slice[],
  currency: string,
): (value: unknown, name: unknown) => [string, string] {
  return (value, name) => {
    const label = String(name);
    const amount = formatMinor(Number(value), currency);
    const share = slices.find((item) => item.name === label)?.percentage;
    return [share === undefined ? amount : `${amount} (${formatShare(share)})`, label];
  };
}
