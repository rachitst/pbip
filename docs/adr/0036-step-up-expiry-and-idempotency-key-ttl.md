# ADR 0036: Unanswered step-ups expire; idempotency keys expire and are purged
- Status: Accepted (under owner's standing approval, 2026-11-03)
- Date: 2026-11-03
- Deciders: Architecture Team (issue #314, gaps found by the LLD, TASK-027)

## Context
The LLD (section 6, items 2 and 4) found two loose ends in the money path:

1. A transfer in `PENDING_STEP_UP` waits for the customer's TOTP code with no deadline. Nothing is held while it
   waits (the money moves only when a correct code arrives), but the transfer counts against the customer's daily
   limit (`_PENDING_OR_DONE`) and carries a fraud decision that grows stale: a code typed hours later would post
   money on a risk assessment made for a different moment.
2. `idempotency_keys.expires_at` is written (`now + IDEMPOTENCY_TTL_HOURS`, default 24) but never read and never
   purged, so a key replays forever and the table only grows.

Both fixes add background work to the existing Celery beat, which AGENTS.md rule 5 treats as an architectural
change, hence this ADR.

## Decision

### 1. Step-up expiry: decline after `STEP_UP_EXPIRY_MINUTES` (default 15)
* New setting `step_up_expiry_minutes` (`core/config.py`, validated 1..1440, default **15**). Fifteen minutes is
  long enough to find a phone and open an authenticator app, including a retry after a wrong code, and short
  enough that an abandoned transfer stops counting against the day's limit and no stale fraud decision is completed
  much later. It is a policy choice, not a measured figure; operators can change it per environment.
* New beat entry `expire-stale-step-ups` -> task `pbip.transfers.expire_stale_step_ups`, every 60 seconds (same
  cadence as `execute-scheduled-transfers`). The query uses `ix_transactions_status` and is empty almost always.
  Runner: `payments/step_up_expiry.py`, `now` injected (tests pass instants; nothing sleeps), batches of
  `step_up_expiry_batch_size` (default 200), each transfer in **its own session and transaction**.
* One transfer = one use case, in the AGENTS.md lock order: the customer's advisory lock (the same first lock
  `confirm_step_up` takes), then the transaction row `FOR UPDATE`, re-checked (still `PENDING_STEP_UP` and still
  overdue), then `status = DECLINED`, `decline_reason = STEP_UP_EXPIRED`, outbox `TRANSFER_DECLINED` (the existing
  bell: "Your transfer of ... could not be completed"), audit `TRANSFER_STEP_UP_EXPIRED` (actor `None`,
  `details.actor = "scheduler"`), one commit.
* **No ledger call.** A `PENDING_STEP_UP` transfer has no hold and no entries (LLD section 4.1), so there is nothing
  to release; balances are untouched (asserted by the tests). `DECLINED` with a reason reuses the existing status
  and CHECK, so no schema change and no new customer-facing state; the customer sees the usual declined message,
  never the reason.
* The deadline is enforced at the request too: `confirm_step_up` answers 409 for a transfer past the timeout even
  if the job has not run yet, so the deadline does not depend on beat latency.
* A confirmation racing the job is serialised by the user lock: whoever is second sees the other's result (job
  `SKIPPED`, customer 409). Escalated (`PENDING_REVIEW`) and posted transfers are never touched.

### 2. Idempotency keys: expired means unused; purged daily
* Semantics: a key answers for `IDEMPOTENCY_TTL_HOURS` after its first use (the configured TTL and the
  `expires_at` column state this intent; ADR-0003 is silent on it). After that it counts as **unused**: the same
  key starts a new request, and a different body with it is not a reuse error. This is the common industry
  semantics (keys are a retry window, not a permanent record).
* Mechanism: `ledger.discard_expired_idempotency_key` deletes the caller's own expired row. Every
  `_begin_idempotent` (payments, bills, loans, wealth) and the step-up replay lookup call it **after the per-user
  advisory lock and before the key insert**, so the lock order is unchanged and no other request of that user can
  be reading or claiming the key meanwhile.
* Purge: new beat entry `purge-expired-idempotency-keys` -> task `pbip.ledger.purge_expired_idempotency_keys`,
  daily at 04:15 UTC (after the nightly analytics jobs). `ledger/key_purge.py` deletes in batches of
  `idempotency_purge_batch_size` (default 1000, at most 100 batches per run), each its own short transaction, with
  `FOR UPDATE SKIP LOCKED` so a row a live request is discarding is left for the next run. Migration `0025` adds
  `ix_idempotency_keys_expires_at` for it (downgrade drops the index). The purge never touches transactions.
* Background jobs keep their own guards beyond the TTL: a scheduled occurrence moves `next_run_date` on, a matured
  deposit is `CLOSED` with a unique `closure_transaction_id` (ADR-0023, ADR-0029). Tested for the scheduled engine.
* `idempotency_ttl_hours` is now validated (1..720).

## Standing-approval conditions
1. **No weakening of security, auth, MFA, RBAC, BOLA or audit:** expiry declines (never posts); the step-up
   deadline only adds a refusal; both jobs write audit or structured logs; no endpoint, role or policy changes.
   A key retried after 24 hours starts a new request; the web app makes one key per intent and retries at once.
2. **Money moves only via `ledger/service.py`, standard lock order, idempotent, concurrency-tested:** the expiry
   moves no money; it takes the user lock then the row lock and re-checks, so re-delivery is harmless;
   `test_step_up_completion_racing_the_expiry_job_settles_exactly_once` races it against a confirmation on real
   PostgreSQL threads.
3. **Migrations:** one index migration `0025` chained on `0024`, with a working downgrade (up, down, up and
   `alembic check` run locally).
4. **No new external service, paid dependency or data leaving the stack:** existing beat and worker only.
5. **No fake data, invented rates or metrics:** the 15-minute default is a stated policy choice, configurable.
6. **Within scope:** FR-22 (idempotent money-moving requests) and the step-up flow (TASK-037); issue #314.

## Alternatives considered
* **A new `EXPIRED` transaction status:** clearer in reports, but needs a CHECK migration, web and contract
  changes for a state the customer sees exactly like a decline. Rejected for now.
* **Expire only at the request (no job):** leaves abandoned transfers counted against the daily limit and stuck.
* **Escalate an expired step-up to review instead of declining:** would hold the customer's money for an analyst
  for a transfer the customer abandoned. Rejected.
* **Keys that never expire:** the current behaviour; unbounded table growth and a stale replay window.

## Consequences
* An unanswered step-up is declined 15 minutes after creation (within a minute of the job), audited and notified.
* `idempotency_keys` stays bounded to about one TTL of traffic.
* Two more beat entries; both are idempotent and cheap.
