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
 * Bounds (the API allows 25 events per batch, ADR-0055, and 120 batches per minute per client IP):
 * - a batch carries at most MAX_BATCH_SIZE events;
 * - at most MAX_BATCHES_PER_MINUTE batches are sent in any 60 s, and one at a time;
 * - the buffer keeps at most MAX_BUFFERED_EVENTS; when full, the OLDEST events are dropped.
 *
 * Consent (ADR-0057): nothing leaves the browser until the customer's recorded analytics consent
 * is known to be on. While it is still loading, events wait in the buffer; a refusal clears it.
 *
 * Session length (ADR-0057): while the page is visible and the customer pointed, typed or
 * scrolled in the last ACTIVE_WINDOW_MS, a ``session_heartbeat`` is recorded every
 * HEARTBEAT_INTERVAL_MS, so a session lasts until the customer stops using it, not until the
 * last click. At most one event a minute, inside the bounds above. */
export const FLUSH_INTERVAL_MS = 10_000;
export const HEARTBEAT_INTERVAL_MS = 60_000;
export const ACTIVE_WINDOW_MS = 5 * 60_000;
const ACTIVITY_EVENTS = ["pointerdown", "keydown", "scroll"] as const;
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
  /** null: the recorded consent is not known yet (events are held, nothing is sent). */
  private consent: boolean | null = null;
  private lastActivity = 0;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
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
    this.lastActivity = this.now();
    this.timer = setInterval(this.flush, FLUSH_INTERVAL_MS);
    this.heartbeatTimer = setInterval(this.heartbeat, HEARTBEAT_INTERVAL_MS);
    window.addEventListener("pagehide", this.flush);
    document.addEventListener("visibilitychange", this.onVisibility);
    for (const name of ACTIVITY_EVENTS) {
      window.addEventListener(name, this.onActivity, { passive: true, capture: true });
    }
  }

  /** Leave the customer area or sign out: unsent events are discarded, never sent afterwards. */
  stop(): void {
    this.active = false;
    this.buffer = [];
    this.page = null;
    this.sessionId = null;
    this.consent = null; // the next customer's consent is loaded again
    if (this.timer !== null) clearInterval(this.timer);
    if (this.heartbeatTimer !== null) clearInterval(this.heartbeatTimer);
    this.timer = null;
    this.heartbeatTimer = null;
    window.removeEventListener("pagehide", this.flush);
    document.removeEventListener("visibilitychange", this.onVisibility);
    for (const name of ACTIVITY_EVENTS) {
      window.removeEventListener(name, this.onActivity, { capture: true });
    }
  }

  /** The customer's recorded analytics consent (and the browser's signal); refusing it also
   * discards the buffer. Until it is set nothing is sent. */
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
      if (type !== "session_heartbeat") this.lastActivity = this.now();
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
    if (this.consent !== true) return; // still waiting for the recorded consent
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

  private readonly onActivity = (): void => {
    this.lastActivity = this.now();
  };

  /** One heartbeat while the page is visible and was used recently; never while idle. */
  private readonly heartbeat = (): void => {
    if (document.visibilityState !== "visible") return;
    if (this.now() - this.lastActivity > ACTIVE_WINDOW_MS) return;
    this.track("session_heartbeat");
  };

  private collecting(): boolean {
    return this.active && this.consent !== false && this.signedIn();
  }

  private withinRate(): boolean {
    const since = this.now() - MINUTE_MS;
    this.sentAt = this.sentAt.filter((t) => t > since);
    return this.sentAt.length < MAX_BATCHES_PER_MINUTE;
  }
}

/** Global Privacy Control (a browser-level "do not sell or share" signal) is treated as refusing
 * analytics, whatever the customer's recorded consent (ADR-0057) says. */
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
