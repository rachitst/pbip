# ADR 0060: Admit no more requests than the pool has connections, and one connection per request
- Status: Accepted (under owner's standing approval, 2026-11-08)
- Date: 2026-11-08
- Deciders: Architecture Team (TASK-079; follow-up to ADR-0045 "the API scales with replicas")

## Context
The k6 load suite (`infra/k6/load.js`) on the staging VM (2 OCPU Arm, two API replicas, pool 10 + 10 per
process) collapsed at 200 VUs: 3.8% errors, every route group at p95 = 30 s, the CPU mostly idle, nginx
`upstream timed out`, and 255 `QueuePool limit of size 10 overflow 10 reached, connection timed out, timeout
30.00` in the API logs. At 50 and 100 VUs the CPU was saturated and nothing failed. Idle CPU with pool timeouts is
starvation, not slowness. Money invariants held at every step.

`apps/api/tests/integration/test_db_pool_starvation.py` reproduces it against the real PostgreSQL with the
production shape scaled down (40 worker threads and 20 connections become 2 threads and 1 connection). Two
mechanisms were proven; both fail on `main` and pass with this change:

1. **A thread/connection cycle.** FastAPI runs every sync dependency, the endpoint and the response validation as
   separate worker-thread calls (anyio's default limiter: 40 threads). The request session checks a connection out
   in the auth dependency (`access_token_is_current`) and keeps it until the session is closed after the
   response. Between those calls the request holds a connection but no thread. Once more requests are in flight
   than there are connections, every worker thread can be blocked in `pool.connect()` while every connection
   belongs to a request waiting for a thread. Nothing moves until `pool_timeout` (30 s), then the waiters fail
   with 500 and the cycle forms again. The `banking_only` case (accounts, transactions, spending, transfers; no
   assistant) fails 14-15 of 15 requests on `main`.
2. **The assistant stream needed two connections.** `POST /assistant/chat/stream` opened its own session while the
   request's session still held its connection (the comment said the stream outlives the request's
   dependencies, which stopped being true in FastAPI 0.118). With a one-connection pool a single stream ended with
   an `error` event after `pool_timeout`. Measured peak connections per request: accounts, transactions, spending,
   transfer and `/assistant/chat` 1; the stream 2.

Also measured: `/assistant/chat` held its connection idle in a transaction while the language model was called
(seconds with Groq or Ollama).

Not proven here: how much of the 339 ms mean `pg_advisory_xact_lock` wait at 100 VUs was lock holders waiting for
a thread (the cycle above) rather than CPU saturation. The lock order is unchanged.

## Options considered
- **Release the connection after the auth check** (and after every other dependency that reads the database).
  Rejected as the fix: it narrows the window but cannot close it. A read-only endpoint keeps its transaction open
  when it returns, and FastAPI validates the response in another worker-thread call, so the cycle still forms
  there. Each early release costs a `ROLLBACK` plus a new checkout with a `pool_pre_ping` round trip per request on
  a CPU-bound host. It would leave the revocation check (ADR-0021) exactly as strict; it is simply not enough.
- **Bigger pool or more threads.** Rejected: the cycle needs only more requests in flight than connections, so
  any finite size starves under enough load. 2 replicas x 20 already uses 40 of PostgreSQL's 100 connections.
- **Async endpoints / an async driver.** Rejected: a rewrite of every module.
- **Admission control sized to the pool, plus one connection per request.** Chosen.

## Decision
1. `core/admission.py` `DatabaseAdmissionMiddleware`, the innermost middleware: at most
   `DB_POOL_SIZE + DB_MAX_OVERFLOW` requests are inside the app per process. The rest wait on the event loop,
   holding neither a worker thread nor a connection, in arrival order. A request that cannot get in within
   `DB_POOL_TIMEOUT_SECONDS` (the same budget it had for a connection) gets **503 problem+json**
   (`code: service_busy`, `Retry-After: 1`) and `db_admission_rejected_total` is incremented. The slot is held
   until the app returns, which is after FastAPI has closed the request's session. `/api/health/live` and
   `/metrics` never touch the database and are exempt.
2. **Every request uses at most one connection.** The SSE stream now runs on the request's own session (FastAPI
   closes request-scoped dependencies after a streamed response has been sent), through the same
   `get_assistant_service` as `/chat`. `test_each_request_type_holds_at_most_one_connection` pins it.
3. **No connection is held while the language model is called:** the assistant ends the auth lookup's read-only
   transaction before the model call (the tool already ran in its own read-only transaction afterwards).

With requests inside <= connections and <= 1 connection each, a checkout never waits, so no worker thread blocks
on the pool and the cycle cannot form. Requests waiting for the per-user advisory lock still hold their slot;
the holder is inside, has its connection and finishes.

Pool defaults are unchanged: 2 replicas x (10 + 10) = 40, the Celery worker (prefork, one session per task) and
beat a handful more, well under `max_connections = 100`.

## Consequences
- Under overload a caller gets a fast, honest 503 after the wait budget instead of a 500 after a pool timeout or
  an nginx 504. With the default 30 s budget nginx's `proxy_read_timeout 30s` may still answer first; lowering
  `DB_POOL_TIMEOUT_SECONDS` below it is a deployment choice, not part of this change.
- No security, authentication, MFA, RBAC or audit behaviour changes. The per-request token revocation check
  (ADR-0021) still runs on every request, uncached, on the request's session. Rate limits still apply.
- No ledger, lock-order, migration or API-contract change; no new dependency or service.
- A slow request (a model call, an Argon2 login) occupies a slot as it occupied a connection before.
- Any future code path that opens a second session or `engine.connect()` inside a request breaks the invariant;
  the per-request connection test catches the request types it covers, and this ADR is the rule for the rest.
