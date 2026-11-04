import type { components } from "../api/schema";

/** The web clickstream event catalogue (TASK-112, FR-50; docs/analytics/clickstream-events.md).
 *
 * Every event the web app may send is listed here with the ONLY property keys it may carry and the
 * values each key accepts. Anything else (a key not listed, a value outside its set) makes the
 * event invalid and it is dropped before it is buffered, so no personal data, amount, account
 * number or free text can leave the browser through this channel even if a caller passes it. */

export type TelemetryEvent = components["schemas"]["TelemetryEventIn"];
export type TelemetryEventType = components["schemas"]["TelemetryEventType"];
type LoanType = components["schemas"]["LoanProductOut"]["loan_type"];
type AccountType = components["schemas"]["OpenAccountType"];
/** Catalogue id of a recommended product (``pbip_ml.recommendations.PRODUCT_CATALOGUE``). */
export type RecommendedProductId = components["schemas"]["ProductRecommendationItem"]["product_id"];

/** How a transfer was started; never the amount, payee, biller or reference. */
export const TRANSFER_KINDS = ["TRANSFER", "BILL_PAYMENT", "SCHEDULED"] as const;
export type TransferKind = (typeof TRANSFER_KINDS)[number];
/** What was searched; never the search terms. */
export const SEARCH_SCOPES = ["TRANSACTIONS"] as const;
export type SearchScope = (typeof SEARCH_SCOPES)[number];

const ACCOUNT_TYPES: readonly AccountType[] = ["SAVINGS", "CHECKING", "CURRENT"];
const LOAN_TYPES: readonly LoanType[] = ["PERSONAL", "AUTO", "HOME"];
/** The filter form has at most this many fields; a count above it is not a real search. */
export const MAX_FILTER_COUNT = 10;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

type Rule = (value: unknown) => boolean;
const oneOf =
  (allowed: readonly string[]): Rule =>
  (value) =>
    typeof value === "string" && allowed.includes(value);
const smallCount: Rule = (value) =>
  typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= MAX_FILTER_COUNT;
const uuid: Rule = (value) => typeof value === "string" && UUID_RE.test(value);

/** The allow-list: event type -> the exact property keys it carries and how each is checked. */
export const EVENT_PROPERTIES: Record<TelemetryEventType, Readonly<Record<string, Rule>>> = {
  page_view: {},
  account_viewed: { account_type: oneOf(ACCOUNT_TYPES) },
  transfer_initiated: { transfer_kind: oneOf(TRANSFER_KINDS) },
  loan_calculator_used: { loan_type: oneOf(LOAN_TYPES) },
  search_performed: { search_scope: oneOf(SEARCH_SCOPES), filter_count: smallCount },
  product_clicked: { product_id: uuid },
  // ADR-0057: sent about once a minute while the customer area is visible and in use, so a
  // session's length is measured; it carries nothing.
  session_heartbeat: {},
};

/** Top-level keys of a sent event; the API rejects unknown keys too (``extra="forbid"``). */
export const EVENT_KEYS = [
  "event_id",
  "event_type",
  "event_timestamp",
  "session_id",
  "page_url",
  "properties",
] as const;

export type EventProperties = Record<string, string | number>;

/** True when ``properties`` has exactly the keys the catalogue lists for ``type``, each valid. */
export function propertiesAllowed(type: TelemetryEventType, properties: EventProperties): boolean {
  const rules = EVENT_PROPERTIES[type];
  const keys = Object.keys(properties);
  const expected = Object.keys(rules);
  if (keys.length !== expected.length) return false;
  return keys.every((key) => {
    const rule = rules[key];
    return rule !== undefined && rule(properties[key]);
  });
}

/** A route template the API accepts as ``page_url`` (its pattern: a path, no query or fragment). */
const PAGE_URL_RE = /^\/[A-Za-z0-9/_.:-]*$/;
export function pageUrlAllowed(template: string): boolean {
  return template.length <= 256 && PAGE_URL_RE.test(template);
}
