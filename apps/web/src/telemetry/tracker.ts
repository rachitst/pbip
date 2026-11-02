import { api, hasAccessToken } from "../api/client";
import type { EventBatchRequest } from "../api/client";
import { propertiesAllowed } from "./catalogue";
import type {
  EventProperties,
  RecommendedProductId,
  SearchScope,
  TelemetryEvent,
  TelemetryEventType,
  TransferKind,
} from "./catalogue";

/** Web clickstream sender (TASK-112). Analytics must never cost the customer anything: track()
 * only appends to an in-memory buffer and never throws; batches leave on a timer and when the tab
 * is hidden or closed; every bound below is fixed, and a batch that fails is dropped, not retried.
 *
 * Bounds (the API allows 100 events per batch and 120 batches per minute per client IP):
 * - a batch carries at most MAX_BATCH_SIZE events;
 * - at most MAX_BATCHES_PER_MINUTE batches are sent in any 60 s, and one at a time;
 * - the buffer keeps at most MAX_BUFFERED_EVENTS; when full, the OLDEST events are dropped. */
export const FLUSH_INTERVAL_MS = 10_000;
export const MAX_BATCH_SIZE = 25;
export const MAX_BUFFERED_EVENTS = 100;
export const MAX_BATCHES_PER_MINUTE = 10;
const MINUTE_MS = 60_000;

export type SendBatch = (batch: EventBatchRequest) => Promise<unknown>;

export interface TrackerOptions {
  send: SendBatch;
  /** Whether a user is signed in; nothing is collected or sent when this is false. */
  signedIn: () => boolean;
  now?: () => number;
  newId?: () => string;
}

export class ClickstreamTracker {
  private readonly send: SendBatch;
  private readonly signedIn: () => boolean;
  private readonly now: () => number;
  private readonly newId: () => string;
  private buffer: TelemetryEvent[] = [];
  private active = false;
  private consent = true;
  private page: string | null = null;
  private sessionId: string | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private inFlight = false;
  private sentAt: number[] = [];

  constructor(options: TrackerOptions) {
    this.send = options.send;
    this.signedIn = options.signedIn;
    this.now = options.now ?? Date.now;
    this.newId = options.newId ?? (() => crypto.randomUUID());
  }

  /** Begin collecting for a signed-in customer: one browser session id until stop(). */
  start(): void {
    if (this.active) return;
    this.active = true;
    this.sessionId = this.newId();
    this.timer = setInterval(this.flush, FLUSH_INTERVAL_MS);
    window.addEventListener("pagehide", this.flush);
    document.addEventListener("visibilitychange", this.onVisibility);
  }

  /** Leave the customer area or sign out: unsent events are discarded, never sent afterwards. */
  stop(): void {
    this.active = false;
    this.buffer = [];
    this.page = null;
    this.sessionId = null;
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    window.removeEventListener("pagehide", this.flush);
    document.removeEventListener("visibilitychange", this.onVisibility);
  }

  /** The customer's analytics consent decision; withdrawing it also discards the buffer. */
  setConsent(granted: boolean): void {
    this.consent = granted;
    if (!granted) this.buffer = [];
  }

  /** The current page's route template (attached to every event), or null for an unknown page. */
  setPage(template: string | null): void {
    this.page = template;
  }

  /** Record one event. Never throws: an event outside the catalogue is dropped. */
  track(type: TelemetryEventType, properties: EventProperties = {}): void {
    try {
      if (!this.collecting() || this.sessionId === null) return;
      if (!propertiesAllowed(type, properties)) return;
      const event: TelemetryEvent = {
        event_id: this.newId(),
        event_type: type,
        event_timestamp: new Date(this.now()).toISOString(),
        session_id: this.sessionId,
        properties: { ...properties },
      };
      if (this.page !== null) event.page_url = this.page;
      this.buffer.push(event);
      const excess = this.buffer.length - MAX_BUFFERED_EVENTS;
      if (excess > 0) this.buffer.splice(0, excess);
    } catch {
      // Analytics must never break the page.
    }
  }

  /** Send the oldest buffered events, if allowed now. Failures are dropped without retry. */
  readonly flush = (): void => {
    if (!this.collecting()) {
      this.buffer = [];
      return;
    }
    if (this.buffer.length === 0 || this.inFlight || !this.withinRate()) return;
    const events = this.buffer.splice(0, MAX_BATCH_SIZE);
    this.sentAt.push(this.now());
    this.inFlight = true;
    try {
      void this.send({ events })
        .catch(() => undefined)
        .finally(() => {
          this.inFlight = false;
        });
    } catch {
      this.inFlight = false;
    }
  };

  /** Events waiting to be sent (for tests and diagnostics; a copy). */
  pending(): TelemetryEvent[] {
    return [...this.buffer];
  }

  private readonly onVisibility = (): void => {
    if (document.visibilityState === "hidden") this.flush();
  };

  private collecting(): boolean {
    return this.active && this.consent && this.signedIn();
  }

  private withinRate(): boolean {
    const since = this.now() - MINUTE_MS;
    this.sentAt = this.sentAt.filter((t) => t > since);
    return this.sentAt.length < MAX_BATCHES_PER_MINUTE;
  }
}

/** Global Privacy Control (a browser-level "do not sell or share" signal) is treated as refusing
 * analytics. PBIP records no analytics consent for customers yet (checked: customers, profile,
 * ADR-0015, whose consent covers the date of birth for recommendations only). */
export function browserAllowsAnalytics(): boolean {
  const gpc = (navigator as Navigator & { globalPrivacyControl?: boolean }).globalPrivacyControl;
  return gpc !== true;
}

export const clickstream = new ClickstreamTracker({
  send: api.sendEvents,
  signedIn: hasAccessToken,
});

// The key interactions of the catalogue (docs/analytics/clickstream-events.md). Each carries a
// category from a fixed list, never an id, amount, name, reference or search term.

export function trackAccountViewed(accountType: string): void {
  clickstream.track("account_viewed", { account_type: accountType });
}

export function trackTransferInitiated(kind: TransferKind): void {
  clickstream.track("transfer_initiated", { transfer_kind: kind });
}

export function trackLoanCalculatorUsed(loanType: string): void {
  clickstream.track("loan_calculator_used", { loan_type: loanType });
}

export function trackSearchPerformed(scope: SearchScope, filterCount: number): void {
  clickstream.track("search_performed", { search_scope: scope, filter_count: filterCount });
}

/** A recommendation card was opened (TASK-062 renders the cards); TASK-101 counts these. */
export function trackProductClicked(productId: RecommendedProductId): void {
  clickstream.track("product_clicked", { product_id: productId });
}
