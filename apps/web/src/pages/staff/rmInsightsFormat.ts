import type {
  ChurnRiskLevel,
  InteractionChannel,
  InteractionOutcome,
  UserOut,
} from "../../api/client";
import { countOf } from "../../lib/format";
import type { Tone } from "../../lib/status";

/** The insights endpoints admit RELATIONSHIP_MANAGER and ADMIN (with an MFA-verified session). */
export function canAccessRMInsights(me: UserOut): boolean {
  return me.roles.includes("RELATIONSHIP_MANAGER") || me.roles.includes("ADMIN");
}

/** Segments the offline job assigns (ADR-0012), plus customers it has not segmented yet. The API
 * types `segment` as a string, so an unknown value is shown in sentence case rather than dropped. */
export const SEGMENT_LABEL: Record<string, string> = {
  HIGH_NET_WORTH: "High net worth",
  BUDGET_CONSCIOUS: "Budget conscious",
  FREQUENT_TRANSACTOR: "Frequent transactor",
  DISCRETIONARY_SPENDER: "Discretionary spender",
  INACTIVE_SAVER: "Inactive saver",
  UNSEGMENTED: "Not segmented yet",
};

export function segmentLabel(segment: string): string {
  const known = SEGMENT_LABEL[segment];
  if (known) return known;
  const words = segment.toLowerCase().replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export const CHURN_LABEL: Record<ChurnRiskLevel, { label: string; tone: Tone }> = {
  LOW: { label: "Low", tone: "success" },
  MEDIUM: { label: "Medium", tone: "warning" },
  HIGH: { label: "High", tone: "danger" },
};

/** Typed by the OpenAPI unions: a value the API adds fails the type check. */
export const CHANNEL_LABEL: Record<InteractionChannel, string> = {
  CALL: "Phone call",
  MEETING: "Meeting",
  EMAIL: "Email",
  NOTE: "Note",
};

export const OUTCOME_LABEL: Record<InteractionOutcome, string> = {
  INTERESTED: "Interested",
  NOT_INTERESTED: "Not interested",
  FOLLOW_UP: "Follow-up needed",
  NO_ANSWER: "No answer",
  INFORMATION_SENT: "Information sent",
  ACCOUNT_REVIEW: "Account review",
};

export const PRODUCT_TYPE_LABEL: Record<string, string> = {
  SAVINGS: "Savings",
  LOAN: "Loan or credit",
  INVESTMENT: "Investment",
  INSURANCE: "Insurance",
};

/** Scores in [0, 1] shown on a 0-100 scale; they are rule-based scores, not probabilities. */
export function score100(score: number): number {
  return Math.round(score * 100);
}

/** "Active today", "1 day", "45 days". */
export function inactivityText(days: number): string {
  return days === 0 ? "Active today" : countOf(days, "day");
}

/** Share of the balance lost over 30 days, e.g. "40%" or "No decline". */
export function declineText(rate: number): string {
  return rate === 0 ? "No decline" : `${String(Math.round(rate * 100))}%`;
}
