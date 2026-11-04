// PBIP clickstream ingestion load test (TASK-055, issue #78): sustain >= 500 events/s through
// POST /api/v1/events/batch with the NFR budgets as thresholds.
//
//   uv run python scripts/seed_load.py --users 200          # once, against a LOCAL stack
//   python scripts/dev.py load-events                       # k6 natively or in the grafana/k6 image
//   BASE_URL=https://localhost:8443 EVENTS_PER_SECOND=500 DURATION=2m python scripts/dev.py load-events
//
// A constant-arrival-rate scenario starts ceil(EVENTS_PER_SECOND / BATCH_SIZE) batches per second
// (default 20 batches of 25 events, the API's cap since ADR-0055), each from the next seeded
// customer in turn. Every event is valid against the API schema and the web catalogue
// (apps/web/src/telemetry/catalogue.ts): an allow-listed type, a route template as page_url and
// only the property keys and values the catalogue lists.
// The stack must run with the load-test rate limits (infra/k6/README.md): every batch comes from one IP.
//
// Pass/fail: k6 exits non-zero when a threshold below is crossed. `dropped_iterations` counts batches
// the schedule could not start (no free VU), so a zero count means the target rate was sustained.
// `events_accepted` is the events the API stored (accepted + duplicates); its summary rate is events/s.
// `events_not_recorded` counts events the API discarded because the customer's analytics consent is
// off (ADR-0057); any such event fails the run, since a discard is not the insert being measured.
// setup() turns each load user's consent on (seed_load.py does too), so a stale seed cannot do that.
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";
import exec from "k6/execution";

const BASE_URL = (__ENV.BASE_URL || "http://localhost:8000").replace(/\/$/, "");
const USERS = Number(__ENV.USERS || 20);
const EVENTS_PER_SECOND = Number(__ENV.EVENTS_PER_SECOND || 500);
const BATCH_SIZE = Number(__ENV.BATCH_SIZE || 25); // the API's MAX_BATCH_EVENTS (ADR-0055)
const DURATION = __ENV.DURATION || "1m"; // keep under the 15 min access-token lifetime
const PASSWORD = __ENV.LOAD_PASSWORD || "Load-Test-Password-2026!"; // pragma: allowlist secret (local seed_load.py constant)
const JSON_HEADERS = { "Content-Type": "application/json" };

if (!(BATCH_SIZE >= 1 && BATCH_SIZE <= 25)) throw new Error("BATCH_SIZE must be 1-25");
if (!(EVENTS_PER_SECOND > 0)) throw new Error("EVENTS_PER_SECOND must be positive");
const BATCHES_PER_SECOND = Math.ceil(EVENTS_PER_SECOND / BATCH_SIZE);

const eventsSent = new Counter("events_sent");
const eventsAccepted = new Counter("events_accepted");
const eventsNotRecorded = new Counter("events_not_recorded");

export const options = {
  scenarios: {
    clickstream: {
      executor: "constant-arrival-rate",
      rate: BATCHES_PER_SECOND,
      timeUnit: "1s",
      duration: DURATION,
      // A batch is one request: 10 VUs per batch/s (maxVUs) absorb batches of up to ~10 s before a drop.
      preAllocatedVUs: Math.max(10, BATCHES_PER_SECOND * 4),
      maxVUs: Math.max(50, BATCHES_PER_SECOND * 10),
    },
  },
  setupTimeout: "5m",
  thresholds: {
    // TASK-055 / TASK-078 DoD: error rate < 0.5%, API p95 < 500 ms, every batch answered 202.
    http_req_failed: ["rate<0.005"],
    "http_req_duration{endpoint:events_batch}": ["p(95)<500"],
    "checks{endpoint:events_batch}": ["rate>0.995"],
    // Every scheduled batch started: the target events/s was sustained, not just attempted.
    dropped_iterations: ["count<1"],
    // ADR-0057: a batch discarded for lack of consent is not an insert; fail loudly.
    events_not_recorded: ["count<1"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max", "count"],
};

function uuid4() {
  // RFC 4122 v4 from Math.random: only a client event id for de-duplication, not a security token.
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function email(i) {
  return `load${String(i).padStart(4, "0")}@perf.pbip`;
}

// Deterministic round-robin per VU and iteration: load-test data only, and reproducible across
// runs (CodeQL js/insecure-randomness flagged Math.random here).
let pickCounter = 0;
function pick(values) {
  pickCounter += 1;
  return values[(__VU * 7919 + __ITER * 104729 + pickCounter) % values.length];
}

// The web catalogue (TASK-112): each type with only the property keys and values it may carry.
const EVENT_KINDS = [
  { type: "page_view", page: "/", properties: () => ({}) },
  { type: "page_view", page: "/insights", properties: () => ({}) },
  {
    type: "account_viewed",
    page: "/accounts/:id",
    properties: () => ({ account_type: pick(["SAVINGS", "CHECKING", "CURRENT"]) }),
  },
  {
    type: "transfer_initiated",
    page: "/transfers",
    properties: () => ({ transfer_kind: pick(["TRANSFER", "BILL_PAYMENT", "SCHEDULED"]) }),
  },
  {
    type: "loan_calculator_used",
    page: "/loans",
    properties: () => ({ loan_type: pick(["PERSONAL", "AUTO", "HOME"]) }),
  },
  {
    type: "search_performed",
    page: "/accounts/:id",
    properties: () => ({
      search_scope: "TRANSACTIONS",
      filter_count: pick([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
    }),
  },
  { type: "product_clicked", page: "/", properties: () => ({ product_id: uuid4() }) },
];

function makeBatch(sessionId) {
  const now = new Date().toISOString();
  const events = [];
  for (let n = 0; n < BATCH_SIZE; n++) {
    const kind = pick(EVENT_KINDS);
    events.push({
      event_id: uuid4(),
      event_type: kind.type,
      event_timestamp: now,
      session_id: sessionId,
      page_url: kind.page,
      properties: kind.properties(),
    });
  }
  return { events };
}

function login(i) {
  const res = http.post(
    `${BASE_URL}/api/v1/auth/login`,
    JSON.stringify({ email: email(i), password: PASSWORD }),
    { headers: JSON_HEADERS, tags: { endpoint: "login" } },
  );
  return res.status === 200 ? res.json("access_token") : null;
}

// setup(): one session per seeded customer. Runs once, before the scheduled batches.
export function setup() {
  const users = [];
  for (let i = 0; i < USERS; i++) {
    const token = login(i);
    if (!token)
      throw new Error(
        `login failed for ${email(i)}: is scripts/seed_load.py seeded and the login limit raised?`,
      );
    const consent = http.put(
      `${BASE_URL}/api/v1/customers/me/analytics-consent`,
      JSON.stringify({ granted: true }),
      {
        headers: { ...JSON_HEADERS, Authorization: `Bearer ${token}` },
        tags: { endpoint: "consent" },
      },
    );
    if (consent.status !== 200 || consent.json("granted") !== true)
      throw new Error(`could not turn analytics consent on for ${email(i)} (ADR-0057)`);
    users.push({ i, token });
  }
  return { users };
}

export default function (data) {
  const u = data.users[exec.scenario.iterationInTest % data.users.length];
  const body = makeBatch(`k6-load-${u.i}-${exec.vu.idInTest}`);
  const res = http.post(`${BASE_URL}/api/v1/events/batch`, JSON.stringify(body), {
    headers: { ...JSON_HEADERS, Authorization: `Bearer ${u.token}` },
    tags: { endpoint: "events_batch" },
  });
  eventsSent.add(BATCH_SIZE);
  const ok = check(
    res,
    {
      "events batch: 202, every event stored": (r) =>
        r.status === 202 && r.json("accepted") + r.json("duplicates") === BATCH_SIZE,
    },
    { endpoint: "events_batch" },
  );
  if (res.status === 202 && res.json("not_recorded") > 0)
    eventsNotRecorded.add(res.json("not_recorded"));
  if (ok) eventsAccepted.add(res.json("accepted") + res.json("duplicates"));
}
