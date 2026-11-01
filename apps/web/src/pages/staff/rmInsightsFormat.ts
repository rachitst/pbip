import type { ChurnRiskLevel, CustomerInsightItem, UserOut } from "../../api/client";
import type { Tone } from "../../lib/status";

export interface RMActionItem {
  id: string;
  customer_id: string;
  action_type: "CALL" | "MEETING" | "EMAIL" | "NOTE";
  outcome: string;
  notes: string;
  logged_by: string;
  created_at: string;
}

/** Check if the authenticated user has Relationship Manager or Admin role. */
export function canAccessRMInsights(me: UserOut): boolean {
  return me.roles.includes("RELATIONSHIP_MANAGER") || me.roles.includes("ADMIN");
}

/** Color tone for churn risk status badges. */
export function churnRiskTone(level: ChurnRiskLevel): Tone {
  switch (level) {
    case "LOW":
      return "success";
    case "MEDIUM":
      return "warning";
    case "HIGH":
      return "danger";
  }
}

/** Human readable label for customer segments. */
export function formatSegment(segment: string): string {
  const map: Record<string, string> = {
    HIGH_NET_WORTH: "High Net Worth",
    SALARIED: "Salaried",
    STUDENT: "Student",
    RETIREE: "Retiree",
    YOUNG_PROFESSIONAL: "Young Professional",
    AFFLUENT: "Affluent",
    MASS_MARKET: "Mass Market",
  };
  return (
    map[segment] ??
    segment
      .replace(/_/g, " ")
      .toLowerCase()
      .replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

/** Human readable product name from product code. */
export function formatProductName(product: string | null | undefined): string {
  if (!product) return "None";
  const map: Record<string, string> = {
    PERSONAL_LOAN: "Personal Loan",
    HOME_LOAN: "Home Loan",
    FIXED_DEPOSIT: "Fixed Deposit",
    PREMIUM_CARD: "Premium Credit Card",
    STUDENT_CARD: "Student Credit Card",
    SAVINGS_PLUS: "Savings Plus Account",
    RECURRING_DEPOSIT: "Recurring Deposit",
  };
  return (
    map[product] ??
    product
      .replace(/_/g, " ")
      .toLowerCase()
      .replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

/** Action storage prefix in localStorage. */
const STORAGE_PREFIX = "pbip_rm_actions_";

/** Load all outreach actions logged for a specific customer. */
export function loadCustomerActions(customerId: string): RMActionItem[] {
  try {
    const raw = localStorage.getItem(`${STORAGE_PREFIX}${customerId}`);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed as RMActionItem[];
  } catch {
    return [];
  }
}

/** Save a new outreach action for a customer. */
export function saveCustomerAction(action: Omit<RMActionItem, "id" | "created_at">): RMActionItem {
  const newItem: RMActionItem = {
    ...action,
    id: `act_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
    created_at: new Date().toISOString(),
  };
  try {
    const existing = loadCustomerActions(action.customer_id);
    const updated = [newItem, ...existing];
    localStorage.setItem(`${STORAGE_PREFIX}${action.customer_id}`, JSON.stringify(updated));
  } catch {
    // Local storage failures should fail open without breaking the UI
  }
  return newItem;
}

/** Derive a high-level financial health label from churn/activity indicators. */
export function deriveHealthLabel(item: CustomerInsightItem): {
  label: string;
  tone: Tone;
  description: string;
} {
  if (item.churn_risk_score >= 0.7 || item.balance_decline_rate >= 0.5) {
    return {
      label: "Needs Attention",
      tone: "danger",
      description: "Steep balance decline or prolonged inactivity poses churn risk.",
    };
  }
  if (
    item.churn_risk_score >= 0.35 ||
    item.balance_decline_rate >= 0.25 ||
    item.inactivity_days >= 30
  ) {
    return {
      label: "Moderate Stability",
      tone: "warning",
      description: "Moderate balance change or declining interaction over recent weeks.",
    };
  }
  return {
    label: "Healthy & Active",
    tone: "success",
    description: "Stable balance retention and frequent account interaction.",
  };
}
