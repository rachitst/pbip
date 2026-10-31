import type { FinancialHealthScoreResponse, HealthScoreComponent } from "../api/client";

/** Display labels and tones for the analytics enums. Typed by the OpenAPI unions, so a value the
 * API adds fails the type check instead of reaching the screen as a raw code. */
type Rating = FinancialHealthScoreResponse["rating"];
type Assessment = HealthScoreComponent["assessment"];
export type Tone = "success" | "info" | "warning" | "danger" | "neutral";

export const RATINGS: Record<Rating, { label: string; tone: Tone }> = {
  EXCELLENT: { label: "Excellent", tone: "success" },
  GOOD: { label: "Good", tone: "info" },
  FAIR: { label: "Fair", tone: "warning" },
  NEEDS_ATTENTION: { label: "Needs attention", tone: "danger" },
};

export const ASSESSMENTS: Record<Assessment, { label: string; tone: Tone }> = {
  OPTIMAL: { label: "Optimal", tone: "success" },
  NO_DEBT: { label: "No debt", tone: "success" },
  ADEQUATE: { label: "Adequate", tone: "success" },
  MODERATE: { label: "Moderate", tone: "warning" },
  LOW: { label: "Low", tone: "warning" },
  VOLATILE: { label: "Volatile", tone: "warning" },
  BURDENED: { label: "Burdened", tone: "warning" },
  DEFICIT: { label: "Deficit", tone: "danger" },
  CRITICAL: { label: "Critical", tone: "danger" },
  VULNERABLE: { label: "Vulnerable", tone: "danger" },
  INSUFFICIENT_DATA: { label: "Not enough data yet", tone: "neutral" },
};

/** Category keys from the API are lower-case words ("housing"); show them in title case. */
export function categoryLabel(category: string): string {
  return category
    .split(/[\s_]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export function paymentCount(count: number): string {
  return `${count} ${count === 1 ? "payment" : "payments"}`;
}
