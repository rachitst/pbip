# ADR 0029: Automatic payout of fixed deposits at maturity
- Status: Proposed
- Date: 2026-11-03
- Deciders: Architecture Team (issue #332, follow-up to TASK-064 / ADR-0025)

## Context
ADR-0025 section 5 computes "matured" on read and pays a deposit out only when the customer closes it. Issue #332
asks for automatic payout at maturity: principal plus interest back to the source account through the existing
`FD_CLOSURE` + `FD_INTEREST` postings, idempotent under retries and concurrent runs, audited, with a bell
notification, and a documented rule for a frozen or closed source account. A background job that moves money is
an architectural change (AGENTS.md rule 5), so this ADR comes before the code. ADR-0025's objection to a job ("a
second money-moving path with its own retry semantics") is answered by reusing the closure maths and postings and
by the guards below.

## Decision

### 1. A new task on the existing beat, hourly
New beat entry `pay-out-matured-deposits` -> task `pbip.wealth.pay_out_matured_deposits`, `crontab(minute=5)`
(hourly, UTC). No new deployable or queue. Not folded into `execute-scheduled-transfers` (that would couple
`payments` to `wealth`, ADR-0025 section 5) and not daily: maturity is a bank day in IST, so an hourly run pays a
deposit within about an hour of IST midnight on its maturity date, and a held payout (section 4) is retried within
the hour after the account is reactivated. The query is indexed (`ix_fixed_deposits_status_maturity_date`) and
empty almost every hour.

The runner (`wealth/maturity_service.py`) takes `now` as a parameter (the task passes `utcnow()`; tests pass a fixed
instant, so no test sleeps), selects ACTIVE deposits with `maturity_date <= bank_today(now)` in batches of
`Settings.fd_maturity_payout_batch_size` (default 100; never-held first, then by maturity date and id, so held
deposits cannot starve new ones) and pays each in **its own session and transaction**.

### 2. One deposit = one use case = one transaction, in the AGENTS.md lock order
1. per-user advisory lock (`deposit.user_id`, the same lock every money request takes);
2. the idempotency row `fd-maturity/{deposit_id}` (one key per deposit, request hash with
   `"op": "wealth.fd.maturity_payout"`). `/` is outside the `Idempotency-Key` header pattern, so no client can
   pre-claim or collide with a job key;
3. the deposit row `FOR UPDATE`, re-checked: still `ACTIVE` and matured, else stop (rollback, nothing written);
4. `ledger.pay_out_fixed_deposit` locks the customer account, `SYS-FIXED-DEPOSITS` and `SYS-CLEARING` in sorted
   id order before either header (unchanged), with `channel="SCHEDULED"`. Interest is the deposit's stored
   `maturity_interest` via the same `_closure_interest` as a customer closure, so no maths is duplicated;
5. deposit closed (`closure_kind = MATURED`), audit `FIXED_DEPOSIT_MATURED_PAID_OUT` (actor `None`,
   `details.actor = "scheduler"`), outbox `FIXED_DEPOSIT_MATURED` (bell), stored response on the key, one commit.

### 3. Idempotency: a deposit is paid at most once
* A retried or duplicated task finds the key with a stored response and stops (`REPLAY`); a concurrent run waits
  on the user lock and then does the same.
* A customer closure racing the job is serialised by the user lock and the deposit row lock; whoever is second
  sees `CLOSED` (job: `SKIPPED`, customer: 409).
* The permanent guards outlive the key's TTL: `status = CLOSED` and the unique `closure_transaction_id`.
* A crash before commit leaves nothing (no key, no posting), so the next run pays normally.

Proved by `test_concurrent_payout_runs_pay_each_deposit_exactly_once` and
`test_payout_racing_a_customer_closure_pays_exactly_once` (real threads, real PostgreSQL).

### 4. Failure rule: frozen or closed source account -> hold and retry, notify once
The ledger refuses to credit a non-ACTIVE customer account (`AccountNotActiveError`). The payout transaction is
rolled back entirely (no key, no posting) and a second transaction (user lock, deposit row `FOR UPDATE`, re-check)
sets `fixed_deposits.payout_held_at` the first time only, with audit `FIXED_DEPOSIT_PAYOUT_HELD` and outbox
`FIXED_DEPOSIT_PAYOUT_HELD` (the bell says the payout is held until the account is active again). The deposit
stays `ACTIVE`; the money stays on `SYS-FIXED-DEPOSITS`, the bank's deposit liability, so it is never in suspense
and reconciles as before. Every hourly run retries; once the account is reactivated it is paid in full and
`payout_held_at` is cleared. A **closed** account stays held until operations resolve it (reopening, or a future
payout-account change); the deposit earns nothing after maturity (ADR-0025). We rejected paying to a suspense
account: it would need a new system account and a manual repair flow, and moves the customer's money away from
a contract that already holds it safely.

### 5. Untouched deposits
Deposits already closed early or withdrawn manually are `CLOSED` and never selected. Deposits not yet matured are
not selected. Insufficient funds cannot occur (the debits are system accounts).

### Schema (migration after the head on `main`, renumbered at merge)
`fixed_deposits.payout_held_at timestamptz NULL` with CHECK `payout_held_at IS NULL OR status = 'ACTIVE'`, and
index `ix_fixed_deposits_status_maturity_date (status, maturity_date)` for the poll. Downgrade drops both.

## Alternatives considered
* **Reuse `execute-scheduled-transfers`.** Couples payments to wealth; its occurrence/missed semantics do not fit.
* **Pay to a suspense account when the source account is frozen or closed.** See section 4.
* **Per-deposit try-lock (ADR-0023 R9) instead of the key.** The user lock already serialises runs; the key gives a
  DB-unique record per deposit and a replay result for duplicates.
* **Auto-renewal.** Out of scope: it needs a customer instruction at booking (renew or pay out, which account). A
  later option on top of this job: a `maturity_instruction` column and a renewal posting in the same use case.

## Consequences
* Good: matured deposits pay out without customer action, exactly once, through the unchanged ledger postings.
* A second writer of `FD_CLOSURE`/`FD_INTEREST` exists (the job); both go through the same ledger function.
* `ledger.pay_out_fixed_deposit` gains an optional `channel` argument (default `WEB`, unchanged for closure).
* The deposit page may briefly show "Matured, ready to withdraw" until the next hourly run; a held deposit shows
  the same, and a manual withdrawal is refused while the account is not active (422, as before).
* New attack surface is small (no new endpoint): threat T50.
