# ADR 0040: Outbox relay retries with backoff, dead-letters, and purges processed events
- Status: Accepted (under owner's standing approval, 2026-11-03)
- Date: 2026-11-03
- Deciders: Architecture Team (TASK-026, issue #49)

## Context
`relay_outbox()` (notifications module, Celery beat every 10 s) already polls `outbox_events` with
`FOR UPDATE SKIP LOCKED` and turns each event into an in-app notification. Three gaps remained (issue #49):

1. **No retry.** One event whose handler raised (a malformed payload, an event type an older worker does not
   know) failed the whole batch transaction. The relay re-polled the same oldest events every 10 seconds, so a
   single poison event blocked every notification behind it, indefinitely.
2. **No retention.** Processed rows were kept forever.
3. **Exactly-once was argued, not proven.** Delivery and `processed_at` commit together, but no test killed a
   worker mid-batch, and nothing at the database level stopped a second notification for the same event.

The backlog entry also says "notifications and external webhooks". PBIP has **no external webhook consumer**:
no requirement in `docs/requirements/requirements-specification.md` mentions webhooks (its only outbox mention is
NFR-04, audit logs), no partner system, no URL, no payload contract. A webhook sink would be new outbound data and
a new attack surface (SSRF, payload leakage, secret management) with nobody to receive it.

Background work on the beat, a schema change and a new failure state are architectural (AGENTS.md rule 5), hence
this ADR.

## Decision

### 1. Handler registry
`OUTBOX_HANDLERS: dict[event_type, handler]` in `notifications/service.py`. Every event type the code enqueues maps
to `_deliver_in_app`, today's notification renderer (a unit test asserts every enqueued type has a handler, and
that `PROJECTED_LOW_BALANCE`, written directly by the forecast, has none). An event type without a handler raises
`NoOutboxHandlerError` and is **retried, then dead-lettered**, instead of being silently marked processed as
before: during a rolling deploy a new API can enqueue a type the old worker does not know yet, and the retry window
lets the new worker deliver it. **No external webhook handler is added**, for the reasons above; a future consumer
is one more registry entry behind its own ADR (fixed URL, allow-listed host, signed payload, no PII).

### 2. Retry with exponential backoff, isolated per event
* New columns on `outbox_events`: `attempts` (int, >= 0), `next_attempt_at` (timestamptz, NOT NULL, default
  `now()`), `last_error` (the exception **class name** only; a message could carry payload data) and
  `dead_lettered_at`. CHECK: an event is never both processed and dead-lettered.
* The poll selects `processed_at IS NULL AND dead_lettered_at IS NULL AND next_attempt_at <= now`, ordered by
  `next_attempt_at, created_at`, `SKIP LOCKED`; partial index `ix_outbox_events_due` replaces
  `ix_outbox_events_unprocessed`.
* Each event's handler runs in its own **SAVEPOINT**. An `Exception` rolls back only that savepoint; the event's
  `attempts` goes up and `next_attempt_at = now + min(base * 2^(attempts-1), max)`; the rest of the batch is
  delivered. Defaults (`core/config.py`, validated): `OUTBOX_MAX_ATTEMPTS=6`, `OUTBOX_RETRY_BASE_SECONDS=30`,
  `OUTBOX_RETRY_MAX_SECONDS=3600`, i.e. waits of 30 s, 1, 2, 4 and 8 min, so an event is dead-lettered about
  15.5 minutes after its first failure. Long enough for a rolling deploy or a brief database blip, short enough
  that an operator learns of a real bug the same day. A policy choice, not a measured figure. No jitter: one
  worker, and deterministic tests; jitter is a one-line change if workers multiply.
* `now` and the policy are injected (`relay_outbox(session, now=..., policy=...)`); the Celery task builds the
  policy from settings. No sleeps in tests.

### 3. Dead letters are visible and audited
On the last failed attempt the event gets `dead_lettered_at`, is never polled again, and the relay writes, **in the
same transaction**, an `OUTBOX_EVENT_DEAD_LETTERED` audit row (actor `None`, `details.actor = "scheduler"`, event
id, type, attempts, error class; no payload) plus an `outbox_event_dead_lettered` error log. Each scheduled retry
logs `outbox_delivery_retry_scheduled` (warning). Compliance officers see dead letters in the existing audit-log
screen; operators see them in Loki. Re-driving a dead letter is an operator action (clear `dead_lettered_at`,
reset `attempts`); a staff endpoint for it is **out of scope** here and would need its own RBAC, BOLA and audit
design. Dead letters are not purged.

### 4. Exactly-once delivery
* `notifications.outbox_event_id` (unique, nullable; NULL for notifications written directly). `_deliver_in_app`
  inserts with `ON CONFLICT (outbox_event_id) DO NOTHING`, so a re-delivered event never notifies twice, even if its
  `processed_at` is lost or reset. **No FK**: processed events are purged, the notification stays.
* Delivery and `processed_at` commit in the relay's one transaction. A worker killed before the commit (SIGKILL,
  `SystemExit`, lost connection) leaves every event of the batch pending and every notification rolled back; the
  next run delivers each exactly once. Proven by an in-process crash test (a `BaseException` after two handlers ran)
  and a subprocess that dispatches a batch and SIGKILLs itself before committing.

### 5. Retention: delete, not archive
New daily beat entry `purge-processed-outbox-events` -> `pbip.outbox.purge_processed`, 04:30 UTC (after the 04:15
idempotency-key purge). `notifications/outbox_purge.py` deletes events **processed** more than
`OUTBOX_RETENTION_DAYS` (default 30) ago, in batches of `OUTBOX_PURGE_BATCH_SIZE` (default 1000, at most 100
batches per run), each its own short transaction with `SKIP LOCKED`; partial index `ix_outbox_events_processed_at`
serves it. Pending and dead-lettered events are never deleted. The boundary is strict: an event processed exactly
at the cutoff is kept.

**Why delete rather than move to an archive table:** an outbox event is a delivery instruction, not a record.
The business change lives in its own tables (the ledger, loans, deposits), security-relevant actions in the
hash-chained `audit_logs` (NFR-04), and what the customer was told in `notifications`, which keeps the event id. An
archive table would be a second, unaudited copy of payloads (user ids, masked accounts, amounts) with no reader and
its own retention problem. Thirty days leaves time to investigate a recent delivery question.

## Standing-approval conditions
1. **No weakening of security, auth, MFA, RBAC, BOLA or audit:** no endpoint, role or policy changes; dead letters
   add an audit row; logs carry ids, types and exception class names only; the purge never touches `audit_logs` or
   `notifications`.
2. **Money only via `ledger/service.py`, standard lock order, idempotent, concurrency-tested:** no money moves. The
   relay takes only outbox row locks (`SKIP LOCKED`) and, for a dead letter, the audit chain lock; two concurrent
   relays are raced on real PostgreSQL threads and deliver each event once.
3. **Migrations:** one migration `0027` chained on `0026`, with a working downgrade (up, down, up and
   `alembic check` run locally; a test checks the data path both ways).
4. **No new external service, paid dependency or outbound data:** existing beat and worker; webhooks deliberately
   not added.
5. **No fake data or invented metrics:** retry and retention defaults are stated policy choices, configurable.
6. **Within scope:** TASK-026 acceptance criteria (backlog), issue #49.

## Alternatives considered
* **A minimal, disabled-by-default webhook sink:** no requirement or consumer; it would be untested pretend code
  against nobody, and new outbound data. Rejected (see Context).
* **Celery's own task retry (`autoretry_for`):** retries the whole batch, so a poison event still blocks the rest,
  and the retry state lives in the broker, not next to the event. Rejected.
* **One transaction per event:** isolates failures too, but a commit per event is slower and a crash then leaves a
  half-delivered batch (still exactly-once thanks to the unique key). Savepoints keep one commit per batch.
* **Archive table:** see section 5.
* **Dead-letter after the first failure:** a transient failure or a rolling deploy would lose deliveries to manual
  re-drives.

## Consequences
* A poison event no longer blocks notifications; it is retried six times and then surfaces in the audit log.
* `outbox_events` stays bounded to about 30 days of traffic plus dead letters.
* A retried event can be delivered after newer ones; notifications are independent, so order is not a contract.
* Operators must watch for `OUTBOX_EVENT_DEAD_LETTERED`; there is no re-drive UI.
