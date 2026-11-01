import type { UserOut } from "../../api/client";
import type { Tone } from "../../lib/status";

/** The fraud endpoints admit FRAUD_ANALYST and ADMIN (with an MFA-verified session). */
export function canReviewFraud(me: UserOut): boolean {
  return me.roles.includes("FRAUD_ANALYST") || me.roles.includes("ADMIN");
}

/** One reason from a fraud decision, as the API stores it (fraud_events.reasons). */
export interface FraudReason {
  code: string;
  message: string;
  source: "RULE" | "MODEL" | "ANOMALY";
  contribution: number | null;
}

const SOURCES = new Set(["RULE", "MODEL", "ANOMALY"]);

/** The reasons arrive as untyped JSON objects; keep only well-formed ones. */
export function parseReasons(raw: Record<string, unknown>[]): FraudReason[] {
  return raw.flatMap((r) => {
    const { code, message, source, contribution } = r;
    if (typeof code !== "string" || typeof message !== "string") return [];
    if (typeof source !== "string" || !SOURCES.has(source)) return [];
    return [
      {
        code,
        message,
        source: source as FraudReason["source"],
        contribution: typeof contribution === "number" ? contribution : null,
      },
    ];
  });
}

export const SOURCE_LABEL: Record<FraudReason["source"], string> = {
  RULE: "Bank rule",
  MODEL: "Model",
  ANOMALY: "Unusual pattern",
};

/** Feature names the anomaly explainer cites, in words an analyst reads at a glance. */
const FEATURE_WORDS: Record<string, string> = {
  ratio_to_avg_amount: "Amount compared with the 30-day average",
  velocity_10m: "Transfers in the previous 10 minutes",
  distinct_devices_7d: "Devices used in 7 days",
  log_amount: "Amount (log10 of paise)",
};

/** "Model: amount is 9.0x the customer's 30-day average" -> "Amount is 9.0x ..." */
export function plainMessage(reason: FraudReason): string {
  let text = reason.message.replace(/^(Model|Unusual):\s*/, "");
  for (const [name, words] of Object.entries(FEATURE_WORDS)) {
    if (text.startsWith(name)) text = words + text.slice(name.length);
  }
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Bar width 0-100 for a reason, relative to the strongest reason of the same source. */
export function barWidths(reasons: FraudReason[]): Map<FraudReason, number> {
  const max = new Map<string, number>();
  for (const r of reasons) {
    const v = Math.abs(r.contribution ?? 0);
    max.set(r.source, Math.max(max.get(r.source) ?? 0, v));
  }
  const widths = new Map<FraudReason, number>();
  for (const r of reasons) {
    const top = max.get(r.source) ?? 0;
    widths.set(r, top > 0 ? Math.round((Math.abs(r.contribution ?? 0) / top) * 100) : 0);
  }
  return widths;
}

/** How the contribution is stated: rule points, SHAP log-odds, or an anomaly score 0-1. */
export function contributionLabel(reason: FraudReason): string {
  if (reason.contribution === null) return "";
  if (reason.source === "RULE") return `+${reason.contribution} points`;
  if (reason.source === "MODEL") return `+${reason.contribution.toFixed(2)} (SHAP)`;
  return `${Math.round(reason.contribution * 100)}% unusual`;
}

export function riskTone(score: number): Tone {
  if (score >= 85) return "danger";
  if (score >= 60) return "warning";
  if (score >= 30) return "info";
  return "neutral";
}

export const DECISION_LABEL: Record<string, string> = {
  ALLOW: "Allowed",
  STEP_UP: "Extra verification",
  REVIEW: "Held for review",
  BLOCK: "Blocked",
};

export const ALERT_STATUS_LABEL: Record<string, { label: string; tone: Tone }> = {
  OPEN: { label: "Open", tone: "warning" },
  CONFIRMED_FRAUD: { label: "Confirmed fraud", tone: "danger" },
  FALSE_POSITIVE: { label: "Released", tone: "success" },
};
