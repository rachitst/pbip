/** Shared date formatting so every page shows dates the same way (en-IN, the bank's locale). */
const dateFormat = new Intl.DateTimeFormat("en-IN", { dateStyle: "medium" });
const dateTimeFormat = new Intl.DateTimeFormat("en-IN", {
  dateStyle: "medium",
  timeStyle: "short",
});

const ACCOUNT_TYPE_LABELS: Record<string, string> = {
  SAVINGS: "Savings",
  CURRENT: "Current (checking)",
  CHECKING: "Current (checking)",
};

/** One name per account type on every page (Accounts, Insights). */
export function accountTypeLabel(accountType: string): string {
  return ACCOUNT_TYPE_LABELS[accountType] ?? accountType;
}

export function formatDate(iso: string): string {
  return dateFormat.format(new Date(iso));
}

export function formatDateTime(iso: string): string {
  return dateTimeFormat.format(new Date(iso));
}

const relativeFormat = new Intl.RelativeTimeFormat("en-IN", { numeric: "auto" });
const pluralRules = new Intl.PluralRules("en-IN");
const countFormat = new Intl.NumberFormat("en-IN");

/** Largest whole unit of a span in seconds: [value, unit], or null under a minute. */
function largestUnit(seconds: number): [number, "minute" | "hour" | "day"] | null {
  const s = Math.max(0, seconds);
  if (s < 60) return null;
  if (s < 3600) return [Math.floor(s / 60), "minute"];
  if (s < 86_400) return [Math.floor(s / 3600), "hour"];
  return [Math.floor(s / 86_400), "day"];
}

/** "5 minutes ago", "3 hours ago", "yesterday"; "just now" under a minute. A timestamp slightly
 * in the future (clock skew between server and browser) also reads "just now". */
export function formatRelative(iso: string, now: number = Date.now()): string {
  const span = largestUnit((now - new Date(iso).getTime()) / 1000);
  if (span === null) return "just now";
  return relativeFormat.format(-span[0], span[1]);
}

/** "1 hour", "3 days", "less than a minute": how long something has been waiting. */
export function formatDuration(seconds: number): string {
  const span = largestUnit(seconds);
  if (span === null) return "less than a minute";
  return countOf(span[0], span[1]);
}

/** "1 alert", "3 alerts", "0 alerts"; ``plural`` for irregular words. */
export function countOf(n: number, singular: string, plural = `${singular}s`): string {
  return `${countFormat.format(n)} ${pluralRules.select(n) === "one" ? singular : plural}`;
}
