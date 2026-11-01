# ADR 0021: Scheduled and recurring transfers engine
- Status: Proposed (refined draft for final review)
- Date: 2026-10-31
- Deciders: Architecture Team (TASK-015 / #38, Core Banking & Ledger)

## Context
TASK-015 asks for standing orders: a customer creates a recurring transfer instruction
(`POST /api/v1/transfers/scheduled` with `frequency` and `start_date`), and a Celery Beat job executes
each due occurrence exactly once, with the deterministic idempotency key `sched-{id}-{occurrence_date}`.
A failure for insufficient funds must notify the customer and must not corrupt or wrongly advance the
schedule. Real PostgreSQL tests must prove concurrency and idempotent-retry behaviour.

This is a new table, a new background job that moves money on a customer's behalf, and a change to
the money path in `modules/payments/service.py`, so AGENTS.md rule 5 requires this ADR before code.

Architectural goals, in priority order:
1. **No duplicate or partial money movement.** Every occurrence posts at most once, through the
   existing ledger (`ledger.post_transaction`, `ledger.place_hold`); nothing else touches balances.
2. **Same rules as an interactive transfer.** Ownership, verified payee, per-transfer and daily limits,
   fraud scoring and audit apply unchanged; a scheduled run is not a privileged path.
3. **The existing lock-order contract** (AGENTS.md section 4) holds, extended without new cycles.
4. **One use case = one database transaction**, including the schedule's own state change.
5. **Explainable outcomes** for the customer (notifications) and staff (audit, fraud events).

Existing building blocks this design reuses (on `main` at `89ec2d6`):
* `PaymentsService.create_transfer` - user advisory lock (`pg_advisory_xact_lock(hashtext(user_id))`),
  idempotency row on `uq_idempotency_keys_user_key`, `get_owned_account` / `get_owned_payee`, fraud
  scoring (`BaselineRulesScorer`), daily limit, ledger posting, outbox event, audit, `commit()`.
* `ledger.lock_accounts` - `FOR UPDATE` in sorted id order; `InsufficientFundsError` from
  `post_transaction` and `place_hold`.
* `notifications.enqueue` (transactional outbox) and `relay_outbox` (Beat, every 10 s).
* `worker/celery_app.py` (UTC, `task_acks_late=True`, so a task may run twice) and `session_scope()`.

## Decision

### 1. Data model (`modules/payments/models.py`, new; migration `0011`, down revision `0010`)
`scheduled_transfers`, following AGENTS.md section 6 (UUID PK, `created_at timestamptz`, explicit FKs
with `ondelete`, CHECK constraints for enums and ranges, an index for every FK and filter):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `customer_id` | FK `customers.id` RESTRICT | owner; every read filters by it (BOLA) |
| `user_id` | FK `users.id` RESTRICT | the user the transfer is executed as (idempotency, daily limit, user lock) |
| `from_account_id` | FK `accounts.id` RESTRICT | must be owned and ACTIVE at creation and at every execution |
| `payee_id` | FK `payees.id` RESTRICT, nullable | exactly one of `payee_id` / `to_account_id` (CHECK) |
| `to_account_id` | FK `accounts.id` RESTRICT, nullable | own-account standing order |
| `amount` | BIGINT, CHECK `> 0` | integer minor units |
| `currency` | VARCHAR(3) | must equal the source account currency (INR today) |
| `reference` | VARCHAR(140), nullable | same validation as `TransferRequest.reference` |
| `frequency` | VARCHAR, CHECK IN (`DAILY`,`WEEKLY`,`MONTHLY`) | |
| `status` | VARCHAR, CHECK IN (`ACTIVE`,`PAUSED`,`CANCELLED`,`COMPLETED`) | |
| `start_date` | DATE | the anchor for recurrence (section 3) |
| `end_date` | DATE, nullable, CHECK `end_date IS NULL OR end_date >= start_date` | last date an occurrence may fall on |
| `next_run_date` | DATE | the next occurrence not yet resolved |
| `last_run_date` | DATE, nullable | last occurrence that produced a transaction |
| `last_transaction_id` | FK `transactions.id` SET NULL, nullable | |
| `failure_count` | INTEGER, CHECK `>= 0` | consecutive failed attempts for the current occurrence |
| `last_failure_reason` | VARCHAR(64), nullable | machine code, e.g. `insufficient_funds` |
| `last_failed_occurrence_date` | DATE, nullable | de-duplicates the failure notification (section 8) |
| `pause_reason` | VARCHAR(32), nullable | NULL when not paused; 'CUSTOMER', 'FRAUD_BLOCK', or 'CUSTOMER_ACTION_REQUIRED' when paused (section 2, 6) |
| `created_at` | timestamptz | |

Indexes: `(status, next_run_date)` for the poll, `(customer_id, created_at)` for the list, and one per
remaining FK. The model is registered in `modules/__init__.py` and `alembic/env.py`; the integration
test `clean_db` TRUNCATE list gains `scheduled_transfers`.

CHECK constraints:
* `ck_scheduled_transfers_amount_positive`: `amount > 0`
* `ck_scheduled_transfers_failure_count_non_negative`: `failure_count >= 0`
* `ck_scheduled_transfers_frequency_valid`: `frequency IN ('DAILY', 'WEEKLY', 'MONTHLY')`
* `ck_scheduled_transfers_status_valid`: `status IN ('ACTIVE', 'PAUSED', 'CANCELLED', 'COMPLETED')`
* `ck_scheduled_transfers_destination_xor`: `(payee_id IS NOT NULL AND to_account_id IS NULL) OR (payee_id IS NULL AND to_account_id IS NOT NULL)`
* `ck_scheduled_transfers_end_date_valid`: `end_date IS NULL OR end_date >= start_date`
* `ck_scheduled_transfers_pause_reason_valid`: `(status = 'PAUSED' AND pause_reason IS NOT NULL AND pause_reason IN ('CUSTOMER', 'FRAUD_BLOCK', 'CUSTOMER_ACTION_REQUIRED')) OR (status <> 'PAUSED' AND pause_reason IS NULL)`

### 2. API lifecycle (`modules/payments/router.py`, prefix `/api/v1/transfers`, tag `payments`)
All routes depend on `get_current_principal` (via `get_current_customer`); the instruction is fetched
only through `payments/policy.get_owned_scheduled_transfer(session, customer_id, scheduled_id)`;
non-owned and non-existent are both **404**. Staff (no customer profile) get **403**. The static
`/scheduled...` routes are declared **before** `/{transfer_id}` so `scheduled` is never parsed as a UUID.

| Route | Effect | Status transition |
|---|---|---|
| `POST /scheduled` | validate and create; audit `SCHEDULED_TRANSFER_CREATED` | -> `ACTIVE`, `next_run_date = start_date`, `pause_reason = NULL` |
| `GET /scheduled` | the customer's instructions | - |
| `GET /scheduled/{id}` | one owned instruction | - |
| `DELETE /scheduled/{id}` | cancel; audit `SCHEDULED_TRANSFER_CANCELLED` | `ACTIVE`/`PAUSED` -> `CANCELLED` (terminal), `pause_reason = NULL` |
| `POST /scheduled/{id}/pause` | pause (approved decision 1); audit `SCHEDULED_TRANSFER_PAUSED` | `ACTIVE` -> `PAUSED`, `pause_reason = 'CUSTOMER'` |
| `POST /scheduled/{id}/resume` | resume (approved decision 1); audit `SCHEDULED_TRANSFER_RESUMED` | `PAUSED` -> `ACTIVE`, `pause_reason = NULL` (see R1/R5 below) |

Creation validation: owned ACTIVE source account; destination is a VERIFIED owned payee or a different
owned account (same rules as `_resolve_destination`); `0 < amount <= max_transfer_minor`; currency
matches the source; `start_date >= today (UTC)`; `end_date >= start_date` when given. Creation moves no
money, so it does not take an `Idempotency-Key`; executions do (section 5).

**Lifecycle and Transition Rules:**
* **DELETE response (R2):** returns `200 OK` with the cancelled instruction object so the client UI can
  perform an immediate optimistic update with the confirmed server state.
* **Invalid state transitions (R3):** pausing an already `PAUSED` or `CANCELLED` instruction, or resuming
  an `ACTIVE` or `CANCELLED` instruction, raises `ConflictError` (`409 Conflict`).
* **Resume roll-forward (R4):** if an instruction was paused and its `next_run_date` is in the past
  (`next_run_date < today`), resuming recalculates `next_run_date` to the earliest occurrence `>= today`
  (consistent with the "no backlog" policy in section 7).
* **Fraud BLOCK resume restriction & analyst clearance (R1/R5 - resolved):** when an instruction is
  auto-paused due to a fraud BLOCK, the engine sets `status = 'PAUSED'` and `pause_reason = 'FRAUD_BLOCK'`.
  The customer `POST /scheduled/{id}/resume` endpoint checks `pause_reason`: if it is `'FRAUD_BLOCK'`, it
  strictly rejects the request with `409 Conflict` (detail: "Scheduled transfer is locked following a
  security decline and requires fraud analyst clearance").
  *Explicit authorized clearance:* resolving or closing a fraud alert alone does NOT automatically clear
  the scheduled instruction's fraud lock. Clearing the fraud lock is an explicit authorized staff operation
  requiring `require_roles([FRAUD_ANALYST, ADMIN], mfa=True)` (e.g. `POST /api/v1/fraud/scheduled-transfers/{id}/clear-lock`),
  which sets `pause_reason = 'CUSTOMER'` (keeping `status = 'PAUSED'`) and logs an audit entry.
  *Resuming after clearance:* once explicitly cleared, the instruction remains `PAUSED` until explicitly
  resumed. Upon resuming, the schedule strictly adheres to the no-backlog rule (section 7): if
  `next_run_date < today (UTC)`, `next_run_date` is rolled forward to the earliest occurrence `>= today`.
  Resuming must **never** execute an expired occurrence or create a catch-up backlog.
* **Customer resume policy for CUSTOMER_ACTION_REQUIRED:** when an instruction was auto-paused due to a
  permanent failure (`pause_reason = 'CUSTOMER_ACTION_REQUIRED'`, e.g. source account frozen/closed or
  payee deleted/unverified), the customer may invoke `POST /scheduled/{id}/resume` only AFTER resolving
  the underlying account or payee issue. The resume endpoint re-validates the entire instruction:
  source account must be owned and `ACTIVE`, and destination must be an owned `ACTIVE` account or a
  `VERIFIED` owned payee. If validation fails, resuming is rejected with `422 Unprocessable` / `409 Conflict`
  explaining the unmet requirement. If validation succeeds, `status` becomes `ACTIVE`, `pause_reason` is
  cleared to `NULL`, and `next_run_date` rolls forward to `>= today` if in the past.
* **Customer capacity (R6):** capacity is bounded via configurable application settings
  (`Settings.max_active_scheduled_transfers_per_customer`, e.g. defaulting to 20 in dev/local) rather
  than a hardcoded DB limit, allowing operational tuning without schema changes.
* **List query (R7):** `GET /scheduled` returns an envelope `items: list[ScheduledTransferResponse]`
  ordered by `next_run_date ASC, created_at DESC`. Keyset cursor pagination can be introduced if
  customer volumes require it.

### 3. Recurrence rules (UTC)
All dates are UTC calendar dates (`utcnow().date()`), matching Celery (`timezone="UTC"`) and the daily
limit window in `_enforce_daily_limit`.
* `DAILY`: `d + 1 day`; `WEEKLY`: `d + 7 days`.
* `MONTHLY`: the `start_date` day-of-month in each following month, **clamped** to that month's last
  day, always computed from the anchor (`start_date`), never from the previous clamped date:
  31 Jan -> 28 Feb (29 in a leap year) -> 31 Mar -> 30 Apr.
* When the next occurrence would be after `end_date`, the instruction becomes `COMPLETED`.

### 4. Beat polling and task execution
* Beat entry `execute-scheduled-transfers` -> task `pbip.transfers.execute_scheduled`, every 60 s, in
  `worker/celery_app.py`; the task lives in `worker/tasks.py` and opens its own sessions (heavy imports
  inside the function, following the existing tasks).
* The task selects the ids of `ACTIVE` instructions with `next_run_date <= today`, ordered by
  `(next_run_date, id)`, and processes **each in its own database transaction**, so one instruction's
  failure or lock contention never rolls back or blocks another.
* At most **one occurrence per instruction per run.**
* Because of `task_acks_late`, a run may be repeated; sections 5 and 6 make that harmless.

**Worker batch configuration (R8):** to bound single-tick database work, the task limits fetched due
instructions via configurable `Settings.scheduled_transfers_batch_size` (default 100, aligning with
`notifications.relay_outbox`'s batch size in `notifications/service.py:98`) rather than an arbitrary
hardcoded constant.

### 5. Locking, single transaction boundary and idempotency
Per occurrence, in this order (extends the AGENTS.md section 4 contract without new cycles):
1. **Per-instruction lock (R9)**: `pg_try_advisory_xact_lock(1346521424, hashtext(scheduled_id))`. It
   never waits: if another worker holds it, this run skips the instruction. Because it never blocks it
   cannot take part in a deadlock.
   *Key-space separation evidence:* PostgreSQL documentation (Section 9.27 "Advisory Lock Functions" &
   Table 54.12 "pg_locks") explicitly specifies that the single 64-bit integer space and the two 32-bit
   integer space do not overlap. In PostgreSQL's internal lock manager (`LOCKTAG`), 64-bit advisory
   locks set `objsubid = 1` while two-integer advisory locks set `objsubid = 2`. Because `objsubid` is a
   distinct component of the internal lock tag, a lock on `(int4, int4)` never conflicts with a single
   `bigint` user lock (`_lock_user`), even if their bit patterns coincide. The constant `1346521424` is
   `0x50424950` (ASCII for `"PBIP"`). An automated regression test will be added in the integration test
   suite to continuously verify this non-collision against real PostgreSQL.
2. **User lock**: `_lock_user(user_id)`, the same first blocking lock every money request takes.
3. **Instruction row**: `SELECT ... FOR UPDATE` (populate existing), then re-check `status = ACTIVE` and
   `next_run_date <= today`; otherwise stop (cancelled/paused/advanced meanwhile).
4. **Idempotency row**: key `sched-{scheduled_id}-{occurrence_date:%Y-%m-%d}` (53 characters; within
   `idempotency_keys.key` VARCHAR(255)), with the request hash computed from the instruction.
5. **Accounts** `FOR UPDATE` in sorted id order (inside `ledger`), then the transaction header and entries.
6. **Instruction update** (advance / pause / failure fields), audit and outbox rows, then **one commit**.

Customer-initiated cancel/pause/resume take the user lock and then the row lock, the same order.

**Single transaction boundary.** `PaymentsService.create_transfer` currently commits inside. It is split
into an internal `_execute_transfer(...)` that does everything except `commit()`, and the existing public
`create_transfer` (unchanged HTTP behaviour: `_execute_transfer` + `commit`). The scheduler calls
`_execute_transfer`, updates the instruction in the same session and commits once, so the transfer and the
schedule advance are atomic. Scheduled transactions are created with `channel="SCHEDULED"`
(`transactions.channel` is VARCHAR(10)); the executing request has no client IP (`RequestMeta()`).

**Idempotency.** The deterministic key makes a re-execution of the same occurrence a replay: the stored
response is returned and no second transaction is posted. Combined with step 3 this covers a duplicated
Celery delivery, two concurrent workers, and a retry after a crash before commit (nothing was committed,
so the retry runs normally).

### 6. Outcomes
| Fraud decision / error | Ledger | Instruction |
|---|---|---|
| ALLOW -> `POSTED` | entries posted | advance `next_run_date`; `last_run_date`, `last_transaction_id`; reset failure fields |
| REVIEW -> `PENDING_REVIEW` | hold placed (`place_hold`) | advance (the occurrence is consumed; the analyst resolves the transaction) |
| **STEP_UP** | **converted to REVIEW: hold placed** (approved decision 3) | advance, as REVIEW |
| **BLOCK** -> `DECLINED` | none | **status -> `PAUSED`, customer notified** (approved decision 2) |
| `InsufficientFundsError` | **rolled back** (no transaction, no idempotency row) | section 7 |

STEP_UP cannot be completed by an absent customer, so it reuses the existing behaviour for users without
MFA (`create_transfer` already turns STEP_UP into REVIEW in that case); `_execute_transfer` gains a flag
for non-interactive callers.

**Outcome Details and Execution Failure Handling:**
* **Fraud BLOCK handling (R10 - resolved):** when a scheduled run evaluates to `BLOCK`, a `DECLINED`
  transaction is recorded, `status` becomes `'PAUSED'`, `pause_reason` becomes `'FRAUD_BLOCK'`, and
  `next_run_date` advances to the subsequent cycle. Advancing the date ensures that once the customer
  obtains fraud analyst clearance and resumes the instruction, the engine executes future occurrences
  rather than replaying the declined occurrence.
* **Transient vs permanent failures (R11 - resolved):**
  * *Transient limits:* hitting `LimitExceededError` (daily limit) follows the same-day retry policy as
    insufficient funds (transient retry until end of day UTC; marked missed on `today > D`).
  * *Permanent invalidation:* if the source account becomes `CLOSED` or `FROZEN`, or the payee is deleted
    or loses `VERIFIED` status, retrying is futile. The engine marks `status = 'PAUSED'`, sets
    `pause_reason = 'CUSTOMER_ACTION_REQUIRED'`, logs the failure reason, and enqueues a failure notification
    informing the customer that the transfer schedule requires attention.

### 7. Same-day retry and missed occurrences (approved decision 5)
For an occurrence dated `D` that fails with insufficient funds:
* The ledger work is rolled back. In a **new** transaction (steps 1-3 of section 5 again, re-checking that
  `next_run_date` is still `D`): `failure_count += 1`, `last_failure_reason = "insufficient_funds"`;
  `next_run_date` and `status` are **not** changed. The failure notification is enqueued only when
  `last_failed_occurrence_date != D` (once per occurrence, not once per poll), then that is set to `D`.
* While `today == D`, every poll retries `D`, so a top-up the same day executes the occurrence.
* On the first poll with `today > D` and `D` still unresolved: the occurrence is **missed**. The customer
  is notified, audit `SCHEDULED_TRANSFER_MISSED` is recorded, failure fields are reset, and
  `next_run_date` advances to the next occurrence after `D`. No idempotency row exists for `D`, and `D` is
  never executed later.
* There is no catch-up: missed occurrences are not paid in bulk later.

**Worker/Beat Outage Policy (R12 - resolved in favor of approved D5):**
An occurrence can be attempted and retried **only during its own UTC calendar day**.
If an infrastructure or Celery Beat outage occurs such that an occurrence `D` was never executed during
its calendar date `D`, when the worker recovers and observes `today > D`, the expired occurrence `D` is
**strictly marked missed without execution**. The engine records audit `SCHEDULED_TRANSFER_MISSED`, enqueues
a `SCHEDULED_TRANSFER_MISSED` notification, resets failure tracking fields, and advances `next_run_date`
to the next upcoming occurrence. A worker outage must **never** cause an expired occurrence to execute late,
and there is strictly **no catch-up backlog**.

### 8. Notifications and audit
New outbox event types, rendered by `notifications._notification` (R13):

| Event | When | Link |
|---|---|---|
| `SCHEDULED_TRANSFER_FAILED` | first insufficient-funds failure of an occurrence | `resource_type = NULL`, `account_id = from_account_id` (approved decision 4) |
| `SCHEDULED_TRANSFER_MISSED` | occurrence missed (section 7) | same |
| `SCHEDULED_TRANSFER_PAUSED` | engine paused the instruction after BLOCK | same |

Successful, under-review and declined executions reuse the existing `TRANSFER_POSTED`,
`TRANSFER_UNDER_REVIEW` and `TRANSFER_DECLINED` events (`resource_type = "transaction"`), which the
transfer path already enqueues. Payloads contain ids, amount and currency only (no PII). The
`resource_type` CHECK (`'transaction','security'`) and `NotificationOut` are **unchanged**.

Note: the web bell (`notificationDestination`) navigates only for `transaction` and `security`, so with
`resource_type = NULL` these notifications are marked read without navigating, although `account_id` is
present. Making them open the source account is a web change for TASK-021 (or a follow-up).

Audit (`AuditService.record`, same transaction): `SCHEDULED_TRANSFER_CREATED`, `_CANCELLED`, `_PAUSED`,
`_RESUMED` (customer actions with authenticated user id), plus the existing `TRANSFER_CREATED` for each
execution and `SCHEDULED_TRANSFER_EXECUTION_FAILED`, `_MISSED`, `_AUTO_PAUSED` for engine actions.
For engine-initiated audit entries (R14), `actor_user_id` is set to `None` with `details={"actor": "scheduler", "channel": "SCHEDULED"}`
to accurately record automated background processing without spoofing user-interactive presence.

### 9. Concurrency, security and failure recovery
* **Concurrent workers / duplicate deliveries:** the try-lock skips, the row re-check stops, the
  idempotency key replays; at most one posting per occurrence. Proved by a threaded PostgreSQL test
  (10 workers, one due occurrence -> one transaction, one debit, one advance).
* **Customer cancel/pause racing an execution:** both take user lock then row lock; whichever commits
  first wins, the other sees the new state. Proved by a threaded test.
* **Crash before commit:** nothing persisted; the next poll retries. **Crash after commit:** the row has
  advanced; the next poll does not see the occurrence as due.
* **Overdraft:** impossible beyond existing guarantees: the same `spendable` checks and the
  `ck_accounts_available_non_negative` CHECK apply.
* **Authorization:** the job only executes instructions created through the authorized API; at execution
  it re-validates ownership, account status and payee status against the stored `customer_id`, so a payee
  deleted or unverified after creation is not paid (see R11).
* **Fail safe:** fraud scoring runs on every execution; a scorer failure follows the existing fail-safe
  rule (rules-only, never allow-all).
* **No sensitive data** in logs, metrics labels or notifications (ids, amounts, codes only).
* **Threat model:** new entry for "a background job moving money on a customer's behalf" in
  `docs/security/threat-model.md`.

### 10. Downstream compatibility
* **TASK-021 (#44, web):** list (payee/destination, frequency, `next_run_date`, status), create, cancel
  via `DELETE /api/v1/transfers/scheduled/{id}`, pause/resume. Types come from the regenerated
  `schema.d.ts`.
* **TASK-027 (#50, LLD):** this ADR's state machine (`ACTIVE` <-> `PAUSED`, -> `CANCELLED`,
  -> `COMPLETED`) and the lock sequence in section 5.
* **TASK-043 (#66, end-of-month projection):** read-only service helper
  `upcoming_occurrences(customer_id: UUID, until: date) -> list[UpcomingOccurrence]` on `PaymentsService` (R15).
  Session management is kept internal to the payments service (injected at service instantiation via `PaymentsService(session, ...)`).
  Returns projected upcoming occurrences up to `until` (`scheduled_transfer_id`, `from_account_id`,
  `amount_minor`, `currency`, `occurrence_date`), computed in-memory from active instructions using the
  recurrence rules in section 3. The analytics module never reads payments repository internals.
* **TASK-047 (#70, loans):** loan disbursal creates monthly repayment instructions through a clean service
  method `create_scheduled_transfer(...)` in `modules/payments/service.py` (R16), avoiding any HTTP detour
  or architectural layering violations when loan accounts are set up.

## Alternatives considered
* **Reuse `create_transfer` as is (it commits)** - the transfer and the schedule update would be two
  commits; a crash between them is recovered by the idempotency replay, but it breaks "one use case = one
  transaction" and needs a recovery path. Rejected in favour of the commit split.
* **A separate executor that writes to the ledger directly** - would duplicate limits, fraud scoring and
  payee rules, and risks diverging from interactive transfers. Rejected (AGENTS.md rule 8).
* **`SELECT ... FOR UPDATE SKIP LOCKED` on the instruction instead of an advisory lock** - would take a
  row lock before the user lock, inverting the lock order against customer cancel/pause. The
  non-blocking advisory try-lock satisfies the acceptance criterion without that inversion.
* **Blocking `pg_advisory_xact_lock` per instruction** - workers would queue behind each other for the
  same instruction for no benefit; the try form simply skips.
* **Retry until funds arrive / catch-up payments** - a long-unfunded daily order would pay a backlog in
  one go, surprising the customer. Rejected by approved decision 5.
* **Advance on insufficient funds immediately** - loses a same-day top-up. Rejected by approved decision 5.
* **Add `scheduled_transfer` to the notification `resource_type` CHECK** - needs a migration and a
  notifications contract change outside this task. Rejected by approved decision 4.
* **Native timezone per customer** - all customers are INR/India today and every existing window is UTC;
  per-customer timezones are left for later.

## Consequences
* Customers get standing orders with the same protections as interactive transfers; staff see every
  execution as a normal transaction (`channel = SCHEDULED`) with its fraud event and audit row.
* `payments/service.py` changes (the commit split): CODEOWNERS review and the existing transfer tests
  must stay green unchanged.
* One more Beat job (every 60 s); its runtime is bounded by R8.
* Migration `0008` (down `0007`). TASK-014's open PR #233 also adds a migration and must be renumbered
  after whichever of the two merges first (the single-head test enforces this).
* Must monitor: execution counts by outcome, missed occurrences, and the worker run duration.
