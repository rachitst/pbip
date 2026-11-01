// PBIP load and concurrency suite (TASK-078) with the NFR budgets as thresholds (TASK-102).
//
//   uv run python scripts/seed_load.py --users 200          # once, against a LOCAL stack
//   python scripts/dev.py load                              # k6 natively or in the grafana/k6 image
//   BASE_URL=https://localhost:8443 VUS=100 DURATION=5m python scripts/dev.py load
//
// Traffic mix per iteration (weighted random, seeded per VU):
//   70% reads  - 50% accounts (list, detail, transactions page), 12% analytics, 8% assistant routed intents
//   20% transfers - POST /api/v1/transfers with a fresh Idempotency-Key, plus a replay of 1 in 10
//   10% auth   - login (Argon2id) and a cookie refresh
// The stack must run with the load-test rate limits (infra/k6/README.md): every VU comes from one IP.
//
// Pass/fail: k6 exits non-zero when a threshold below is crossed. The money invariants (no overdraft,
// balanced ledger, no deadlocks) are checked after the run by scripts/load_invariants.py.
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter } from "k6/metrics";
import exec from "k6/execution";

const BASE_URL = (__ENV.BASE_URL || "http://localhost:8000").replace(/\/$/, "");
const USERS = Number(__ENV.USERS || 200);
const VUS = Number(__ENV.VUS || 100);
const DURATION = __ENV.DURATION || "3m";
const THINK_SECONDS = Number(__ENV.THINK_SECONDS || 1);
const PASSWORD = __ENV.LOAD_PASSWORD || "Load-Test-Password-2026!"; // pragma: allowlist secret (local seed_load.py constant)
const JSON_HEADERS = { "Content-Type": "application/json" };
const CSRF = { "X-Requested-With": "pbip-web" };

const transferOutcome = new Counter("transfer_outcomes");
const unexpected = new Counter("unexpected_responses");

export const options = {
  scenarios: {
    banking_mix: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: VUS },
        { duration: DURATION, target: VUS },
        { duration: "10s", target: 0 },
      ],
      gracefulRampDown: "10s",
    },
  },
  setupTimeout: "10m",
  thresholds: {
    // TASK-078 DoD: error rate < 0.5%. Expected business outcomes (fraud 403 BLOCK, 202 hold) are not errors.
    http_req_failed: ["rate<0.005"],
    unexpected_responses: ["count<1"],
    // NFR (section 8): API responses < 500 ms at p95 per route group; login < 3 s.
    "http_req_duration{kind:accounts}": ["p(95)<500"],
    "http_req_duration{kind:analytics}": ["p(95)<500"],
    "http_req_duration{kind:assistant}": ["p(95)<500"],
    "http_req_duration{kind:transfers}": ["p(95)<500"],
    "http_req_duration{endpoint:login}": ["p(95)<3000"],
    "http_req_duration{endpoint:refresh}": ["p(95)<500"],
    checks: ["rate>0.995"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max", "count"],
};

function uuid4() {
  // RFC 4122 v4 from Math.random: only an idempotency key, not a security token.
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function email(i) {
  return `load${String(i).padStart(4, "0")}@perf.pbip`;
}

function login(i) {
  const res = http.post(`${BASE_URL}/api/v1/auth/login`, JSON.stringify({ email: email(i), password: PASSWORD }), {
    headers: JSON_HEADERS,
    tags: { endpoint: "login", kind: "auth" },
  });
  const ok = check(res, { "login: 200 with an access token": (r) => r.status === 200 && !!r.json("access_token") });
  if (!ok) unexpected.add(1, { endpoint: "login", status: String(res.status) });
  return ok ? res.json("access_token") : null;
}

function auth(token) {
  return { headers: { ...JSON_HEADERS, Authorization: `Bearer ${token}` } };
}

// setup(): one session per seeded user, plus the ids each VU needs. Runs once, before the load.
export function setup() {
  const users = [];
  for (let i = 0; i < USERS; i++) {
    const token = login(i);
    if (!token) throw new Error(`login failed for ${email(i)}: is scripts/seed_load.py seeded and the login limit raised?`);
    const accounts = http.get(`${BASE_URL}/api/v1/accounts`, auth(token)).json();
    const payees = http.get(`${BASE_URL}/api/v1/payees`, auth(token)).json();
    users.push({ i, token, accountId: accounts[0].id, payeeId: payees[0].id });
  }
  return { users };
}

function readAccounts(u) {
  const list = http.get(`${BASE_URL}/api/v1/accounts`, { ...auth(u.token), tags: { endpoint: "accounts_list", kind: "accounts" } });
  const detail = http.get(`${BASE_URL}/api/v1/accounts/${u.accountId}`, { ...auth(u.token), tags: { endpoint: "account_detail", kind: "accounts" } });
  const txns = http.get(`${BASE_URL}/api/v1/accounts/${u.accountId}/transactions?limit=25`, {
    ...auth(u.token),
    tags: { endpoint: "transactions", kind: "accounts" },
  });
  check(list, { "accounts: 200": (r) => r.status === 200 });
  check(detail, { "account detail: 200": (r) => r.status === 200 });
  check(txns, { "transactions: 200": (r) => r.status === 200 });
}

// Per-account analytics. /analytics/segment is left out: it is 404 until the weekly segmentation job has run.
const ANALYTICS = ["spending", "trends", "health-score", "forecast"];
function readAnalytics(u) {
  const path = ANALYTICS[Math.floor(Math.random() * ANALYTICS.length)];
  const res = http.get(`${BASE_URL}/api/v1/analytics/${path}?account_id=${u.accountId}`, { ...auth(u.token), tags: { endpoint: `analytics_${path}`, kind: "analytics" } });
  check(res, { "analytics: 200": (r) => r.status === 200 });
}

// Each message names exactly one intent, so the deterministic router answers without the LLM.
const ROUTED = ["What is my balance?", "Show my recent transactions", "How much did I spend last month?"];
function askAssistant(u) {
  const message = ROUTED[Math.floor(Math.random() * ROUTED.length)];
  const res = http.post(`${BASE_URL}/api/v1/assistant/chat`, JSON.stringify({ message }), {
    ...auth(u.token),
    tags: { endpoint: "assistant_chat", kind: "assistant" },
  });
  check(res, { "assistant: 200": (r) => r.status === 200 });
}

// 201 posted, 202 held (step-up/review), 403 blocked by fraud scoring: all correct outcomes.
const TRANSFER_OK = http.expectedStatuses(201, 202, 403);
function transfer(u) {
  const key = uuid4();
  const body = JSON.stringify({
    from_account_id: u.accountId,
    payee_id: u.payeeId,
    amount_minor: 100 + Math.floor(Math.random() * 4900), // 1.00 - 50.00: far below every limit
  });
  const params = { headers: { ...auth(u.token).headers, "Idempotency-Key": key }, tags: { endpoint: "transfer", kind: "transfers" }, responseCallback: TRANSFER_OK };
  const res = http.post(`${BASE_URL}/api/v1/transfers`, body, params);
  const known = [201, 202, 403].includes(res.status);
  transferOutcome.add(1, { status: String(res.status) });
  if (!known) unexpected.add(1, { endpoint: "transfer", status: String(res.status) });
  check(res, { "transfer: 201/202/403": () => known });
  if (known && Math.random() < 0.1) {
    // Idempotent retry: same key and body must return the stored response, never a second posting.
    const replay = http.post(`${BASE_URL}/api/v1/transfers`, body, { ...params, tags: { endpoint: "transfer_replay", kind: "transfers" } });
    check(replay, {
      "replay: same status, same transfer, marked replayed": (r) =>
        r.status === res.status &&
        r.headers["Idempotent-Replayed"] === "true" &&
        (res.status === 403 || r.json("id") === res.json("id")),
    });
  }
}

function authFlow(u) {
  const jar = http.cookieJar();
  jar.clear(BASE_URL);
  const token = login(u.i);
  if (!token) return;
  const res = http.post(`${BASE_URL}/api/v1/auth/refresh`, null, { headers: CSRF, tags: { endpoint: "refresh", kind: "auth" } });
  check(res, { "refresh: 200": (r) => r.status === 200 && !!r.json("access_token") });
}

export default function (data) {
  const u = data.users[(exec.vu.idInTest - 1) % data.users.length];
  const roll = Math.random() * 100;
  if (roll < 50) readAccounts(u);
  else if (roll < 62) readAnalytics(u);
  else if (roll < 70) askAssistant(u);
  else if (roll < 90) transfer(u);
  else authFlow(u);
  sleep(THINK_SECONDS * (0.5 + Math.random()));
}
