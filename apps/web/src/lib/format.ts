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
