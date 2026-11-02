import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setAccessToken } from "../api/client";
import type { EventBatchRequest } from "../api/client";
import { json, urlOf } from "../test-utils";
import clientBatch from "./__fixtures__/client-batch.json";
import { EVENT_KEYS, EVENT_PROPERTIES } from "./catalogue";
import {
  ClickstreamTracker,
  FLUSH_INTERVAL_MS,
  MAX_BATCHES_PER_MINUTE,
  MAX_BATCH_SIZE,
  MAX_BUFFERED_EVENTS,
  browserAllowsAnalytics,
  clickstream,
  trackAccountViewed,
  trackLoanCalculatorUsed,
  trackProductClicked,
  trackSearchPerformed,
  trackTransferInitiated,
} from "./tracker";

const START = Date.parse("2026-11-02T10:00:00.000Z");
const INDEX_SIP = "00000000-0000-4000-a000-000000000005";

function setup(options: { signedIn?: () => boolean; send?: () => Promise<unknown> } = {}) {
  const sent: EventBatchRequest[] = [];
  const send = vi.fn((batch: EventBatchRequest) => {
    sent.push(batch);
    return options.send ? options.send() : Promise.resolve({});
  });
  let n = 0;
  const tracker = new ClickstreamTracker({
    send,
    signedIn: options.signedIn ?? (() => true),
    now: () => Date.now(),
    newId: () => `00000000-0000-4000-8000-${String(++n).padStart(12, "0")}`,
  });
  return { tracker, send, sent };
}

/** Let the send promise's .catch/.finally run (no timers are involved). */
async function settle() {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

describe("web clickstream tracker (TASK-112)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(START);
  });
  afterEach(() => {
    clickstream.stop();
    setAccessToken(null);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("buffers events and sends them as one batch on the flush interval, not before", async () => {
    const { tracker, send, sent } = setup();
    tracker.start();
    tracker.setPage("/accounts");
    tracker.track("page_view");
    tracker.track("account_viewed", { account_type: "SAVINGS" });
    vi.advanceTimersByTime(FLUSH_INTERVAL_MS - 1);
    expect(send).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(send).toHaveBeenCalledTimes(1);
    expect(sent[0]?.events.map((e) => e.event_type)).toEqual(["page_view", "account_viewed"]);
    expect(tracker.pending()).toEqual([]);
    await settle();
    vi.advanceTimersByTime(FLUSH_INTERVAL_MS);
    expect(send).toHaveBeenCalledTimes(1); // nothing new to send: no empty batches
  });

  it("builds exactly the batch the API integration test posts (client-batch.json)", () => {
    const { tracker, sent } = setup();
    tracker.start();
    tracker.setPage("/accounts");
    tracker.track("page_view");
    tracker.track("account_viewed", { account_type: "SAVINGS" });
    tracker.track("search_performed", { search_scope: "TRANSACTIONS", filter_count: 2 });
    tracker.track("transfer_initiated", { transfer_kind: "TRANSFER" });
    tracker.setPage("/loans");
    tracker.track("page_view");
    tracker.track("loan_calculator_used", { loan_type: "PERSONAL" });
    tracker.setPage("/insights");
    tracker.track("product_clicked", { product_id: INDEX_SIP });
    vi.advanceTimersByTime(FLUSH_INTERVAL_MS);
    expect(sent).toEqual([clientBatch]);
  });

  it("flushes when the page is hidden or closed", () => {
    const { tracker, send } = setup();
    tracker.start();
    tracker.track("page_view");
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(send).toHaveBeenCalledTimes(1);
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });

    tracker.track("page_view");
    document.dispatchEvent(new Event("visibilitychange")); // becoming visible does not flush
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("sends at most MAX_BATCH_SIZE events per batch, oldest first", () => {
    const { tracker, sent } = setup();
    tracker.start();
    for (let i = 0; i < MAX_BATCH_SIZE + 5; i++) tracker.track("page_view");
    const first = tracker.pending()[0];
    vi.advanceTimersByTime(FLUSH_INTERVAL_MS);
    expect(sent[0]?.events).toHaveLength(MAX_BATCH_SIZE);
    expect(sent[0]?.events[0]).toEqual(first);
    expect(tracker.pending()).toHaveLength(5);
  });

  it("caps the buffer by dropping the OLDEST events", () => {
    const { tracker } = setup();
    tracker.start();
    for (let i = 0; i < MAX_BUFFERED_EVENTS + 30; i++) {
      vi.setSystemTime(START + i * 1000);
      tracker.track("page_view");
    }
    const pending = tracker.pending();
    expect(pending).toHaveLength(MAX_BUFFERED_EVENTS);
    expect(pending[0]?.event_timestamp).toBe(new Date(START + 30_000).toISOString());
  });

  it("never sends more than MAX_BATCHES_PER_MINUTE batches in any minute, one at a time", async () => {
    const { tracker, send } = setup();
    tracker.start();
    for (let i = 0; i < 30; i++) {
      tracker.track("page_view");
      window.dispatchEvent(new Event("pagehide"));
      await settle();
    }
    expect(send).toHaveBeenCalledTimes(MAX_BATCHES_PER_MINUTE);
    vi.advanceTimersByTime(60_000);
    expect(send).toHaveBeenCalledTimes(MAX_BATCHES_PER_MINUTE + 1);

    const slow = setup({ send: () => new Promise(() => {}) }); // never settles
    slow.tracker.start();
    slow.tracker.track("page_view");
    slow.tracker.flush();
    slow.tracker.track("page_view");
    slow.tracker.flush();
    vi.advanceTimersByTime(FLUSH_INTERVAL_MS * 3);
    expect(slow.send).toHaveBeenCalledTimes(1);
  });

  it("drops a failed batch silently and never retries it", async () => {
    const { tracker, send, sent } = setup({ send: () => Promise.reject(new Error("offline")) });
    tracker.start();
    tracker.track("page_view");
    vi.advanceTimersByTime(FLUSH_INTERVAL_MS);
    await settle();
    expect(send).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(FLUSH_INTERVAL_MS * 5);
    expect(send).toHaveBeenCalledTimes(1);

    tracker.track("account_viewed", { account_type: "CURRENT" });
    vi.advanceTimersByTime(FLUSH_INTERVAL_MS);
    expect(sent[1]?.events.map((e) => e.event_type)).toEqual(["account_viewed"]);

    const throwing = setup({
      send: () => {
        throw new Error("fetch is not available");
      },
    });
    throwing.tracker.start();
    throwing.tracker.track("page_view");
    expect(() => {
      throwing.tracker.flush();
    }).not.toThrow();
    throwing.tracker.track("page_view");
    throwing.tracker.flush();
    expect(throwing.send).toHaveBeenCalledTimes(2); // a synchronous throw does not wedge it
  });

  it("collects and sends nothing while signed out, and drops the buffer at sign-out", () => {
    let signedIn = false;
    const { tracker, send } = setup({ signedIn: () => signedIn });
    tracker.start();
    tracker.track("page_view");
    expect(tracker.pending()).toEqual([]);

    signedIn = true;
    tracker.track("page_view");
    signedIn = false;
    vi.advanceTimersByTime(FLUSH_INTERVAL_MS);
    window.dispatchEvent(new Event("pagehide"));
    expect(send).not.toHaveBeenCalled();
    expect(tracker.pending()).toEqual([]);
  });

  it("does nothing before start() and discards unsent events at stop()", () => {
    const { tracker, send } = setup();
    tracker.track("page_view");
    expect(tracker.pending()).toEqual([]);
    tracker.start();
    tracker.track("page_view");
    tracker.stop();
    expect(tracker.pending()).toEqual([]);
    vi.advanceTimersByTime(FLUSH_INTERVAL_MS * 3);
    window.dispatchEvent(new Event("pagehide"));
    expect(send).not.toHaveBeenCalled();
  });

  it("honours the analytics consent switch: refusing stops collection and clears the buffer", () => {
    const { tracker, send, sent } = setup();
    tracker.start();
    tracker.track("page_view");
    tracker.setConsent(false);
    expect(tracker.pending()).toEqual([]);
    tracker.track("page_view");
    vi.advanceTimersByTime(FLUSH_INTERVAL_MS);
    expect(send).not.toHaveBeenCalled();

    tracker.setConsent(true);
    tracker.track("account_viewed", { account_type: "SAVINGS" });
    vi.advanceTimersByTime(FLUSH_INTERVAL_MS);
    expect(sent.flatMap((b) => b.events.map((e) => e.event_type))).toEqual(["account_viewed"]);
  });

  it("treats the browser's Global Privacy Control signal as refusing analytics", () => {
    expect(browserAllowsAnalytics()).toBe(true);
    Object.defineProperty(navigator, "globalPrivacyControl", { value: true, configurable: true });
    try {
      expect(browserAllowsAnalytics()).toBe(false);
    } finally {
      Reflect.deleteProperty(navigator, "globalPrivacyControl");
    }
  });

  describe("payload allow-list", () => {
    it("drops events with keys or values outside the catalogue", () => {
      const { tracker } = setup();
      tracker.start();
      tracker.track("page_view", { account_number: "409443369528" });
      tracker.track("account_viewed", { account_type: "SAVINGS", amount_minor: 150000 });
      tracker.track("account_viewed", { account_type: "alice@example.com" });
      tracker.track("account_viewed", {});
      tracker.track("transfer_initiated", { transfer_kind: "TRANSFER", reference: "rent" });
      tracker.track("search_performed", { search_scope: "TRANSACTIONS", filter_count: 1.5 });
      tracker.track("search_performed", { search_scope: "TRANSACTIONS", filter_count: 9999 });
      tracker.track("loan_calculator_used", { loan_type: "PERSONAL", amount_minor: 500000 });
      tracker.track("product_clicked", { product_id: "Alice Demo" });
      expect(tracker.pending()).toEqual([]);
    });

    it("every event the helpers send has only the allowed keys and properties", () => {
      setAccessToken("token");
      const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(json(202, {})));
      vi.stubGlobal("fetch", fetchMock);
      clickstream.start();
      clickstream.setPage("/accounts");
      clickstream.track("page_view");
      trackAccountViewed("SAVINGS");
      trackTransferInitiated("BILL_PAYMENT");
      trackLoanCalculatorUsed("HOME");
      trackSearchPerformed("TRANSACTIONS", 3);
      trackProductClicked(INDEX_SIP);
      vi.advanceTimersByTime(FLUSH_INTERVAL_MS);

      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [input, init] = fetchMock.mock.calls[0] ?? [];
      expect(input === undefined ? "" : urlOf(input)).toBe("/api/v1/events/batch");
      const body = JSON.parse(init?.body as string) as EventBatchRequest;
      expect(Object.keys(body)).toEqual(["events"]);
      expect(body.events.map((e) => e.event_type)).toEqual([
        "page_view",
        "account_viewed",
        "transfer_initiated",
        "loan_calculator_used",
        "search_performed",
        "product_clicked",
      ]);
      for (const event of body.events) {
        expect(Object.keys(event).sort()).toEqual([...EVENT_KEYS].sort());
        expect(event.page_url).toBe("/accounts");
        expect(Object.keys(event.properties ?? {}).sort()).toEqual(
          Object.keys(EVENT_PROPERTIES[event.event_type]).sort(),
        );
      }
      expect(body.events[5]?.properties).toEqual({ product_id: INDEX_SIP });
    });

    it("sends with keepalive and the bearer token, and a 401 neither refreshes nor signs out", async () => {
      setAccessToken("token");
      const fetchMock = vi.fn<typeof fetch>(() =>
        Promise.resolve(json(401, { code: "unauthorized", title: "Unauthorized" })),
      );
      vi.stubGlobal("fetch", fetchMock);
      clickstream.start();
      clickstream.track("page_view");
      vi.advanceTimersByTime(FLUSH_INTERVAL_MS);
      await settle();
      expect(fetchMock).toHaveBeenCalledTimes(1); // no /auth/refresh, no retry
      const init = fetchMock.mock.calls[0]?.[1];
      expect(init?.keepalive).toBe(true);
      expect(init?.method).toBe("POST");
      expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer token");
    });
  });
});
