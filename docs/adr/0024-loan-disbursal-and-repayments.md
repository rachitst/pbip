# ADR 0024: Loan applications, ledger disbursal and repayments through the scheduled transfers engine
- Status: Accepted (for the scope of TASK-047 / TASK-051 only; see "Scope of acceptance")
- Date: 2026-11-02
- Deciders: Architecture Team (TASK-047 / #70, TASK-051 / #74)

## Context
TASK-045/046 gave PBIP a fixed loan catalogue (`modules/loans/catalogue.py`), exact EMI arithmetic
(`loans/emi.py`, `Fraction`, integer paise) and explainable eligibility (`loans/service.check_eligibility`:
DTI <= 50% of ledger income, a recent lockout refuses). TASK-047 must turn an eligible request into a real loan:

1. `POST /api/v1/loans/apply` creates the loan and its full repayment schedule in ONE database transaction;
2. the approved loan is disbursed through the ledger: DEBIT the bank's clearing account, CREDIT the customer;
3. the disbursed loan appears in the customer's active loans with its remaining balance;
4. the monthly repayments are linked to the scheduled transfers engine (ADR-0023).

This adds tables, a new kind of money movement, a change to the scheduled transfers engine and a new
customer-facing money-moving endpoint, so AGENTS.md rule 5 requires this ADR before code.

The engine today pays only a verified payee or one of the customer's OWN accounts (`_resolve_destination`), runs
every occurrence through the transfer use case (fraud scoring, daily limit) and records missed occurrences
without catching up. None of that is right for a loan instalment: the destination is the bank itself, the amount
of the last instalment differs from the EMI, and a missed instalment is a debt that must still be collected.

## Decision

### 1. Tables (migration `0016`)
* **`loans`**: UUID PK, `created_at`; `customer_id` -> `customers` (RESTRICT), `user_id` -> `users` (RESTRICT,
  the applicant), `account_id` -> `accounts` (RESTRICT; the disbursal AND repayment account),
  `disbursal_transaction_id` -> `transactions` (RESTRICT, UNIQUE: one disbursal per loan). A snapshot of the
  catalogue terms at approval: `product_id` (catalogue UUID, no FK: the catalogue lives in code), `loan_type`,
  `annual_rate_bps`, `tenure_months`, `principal`, `emi`, `total_interest`, `currency`. `status`
  (`ACTIVE`/`CLOSED`), `closed_at`. CHECKs: enums, `principal > 0`, `emi > 0`, `tenure_months > 0`,
  `annual_rate_bps >= 0`, `total_interest >= 0`, `CLOSED <=> closed_at IS NOT NULL`. Indexes on every FK and
  on `(customer_id, created_at)` (the "my loans" query).
* **`loan_instalments`**: UUID PK, `created_at`; `loan_id` -> `loans` (CASCADE: an instalment has no meaning
  without its loan; loans themselves cannot be deleted while a disbursal or an instruction references them),
  `transaction_id` -> `transactions` (RESTRICT; the repayment that paid it). `seq` (1..n, UNIQUE with
  `loan_id`), `due_date`, `amount`, `principal`, `interest`, `balance_after`, `status` (`DUE`/`PAID`),
  `paid_at`. CHECKs: `amount > 0`, `amount = principal + interest`, non-negative parts and balance,
  `PAID <=> transaction_id AND paid_at present`. The rows are `emi.schedule()` verbatim, so the principal of all
  instalments sums to exactly the amount borrowed (property-tested in TASK-045).
* **`scheduled_transfers.loan_id`** -> `loans` (RESTRICT, UNIQUE: one standing instruction per loan), and the
  destination CHECK becomes "exactly one of `payee_id`, `to_account_id`, `loan_id`" (`num_nonnulls(...) = 1`).
* **`transactions.type`** gains `LOAN_DISBURSAL` and `LOAN_REPAYMENT` (the CHECK constraint is widened; the
  downgrade narrows it and refuses if such rows exist, rather than silently deleting ledger history).

Overdue is derived, not stored: an instalment is overdue when it is `DUE` and its `due_date` is before today (UTC).
Remaining balance = the principal of the unpaid instalments. No status column has to be kept in sync by a job.

### 2. Disbursal posting
New `ledger/service.py` functions `disburse_loan` and `collect_loan_repayment`, each one
`create_transaction` + `post_transaction` with its own transaction type:

| Posting | DEBIT | CREDIT | Type |
|---|---|---|---|
| Disbursal | `SYS-CLEARING` | customer account | `LOAN_DISBURSAL` |
| Repayment | customer account | `SYS-CLEARING` | `LOAN_REPAYMENT` |

Only the ledger writes entries and balances (AGENTS.md section 4). Separate types (not `DEPOSIT`/`TRANSFER`)
matter: analytics counts `DEPOSIT`/`TRANSFER` credits as income (`analytics/repository.INCOME_TYPES`), so a loan
recorded as a deposit would raise the customer's "income" and let them borrow more on the next check.

**Lock order in `apply`** (AGENTS.md section 4, unchanged contract): (1) per-user advisory lock, (2) the
`idempotency_keys` row, (3) eligibility re-run (reads only), (4) accounts `FOR UPDATE` in sorted id order
(customer account + clearing) inside `create_transaction`, BEFORE the transaction header, the `loans` row and
the `scheduled_transfers` row are inserted (each takes KEY SHARE locks on `accounts`). Everything, including the
audit row and the outbox event, commits once. A refusal, an inactive account or any error rolls all of it back.

The eligibility re-run happens AFTER the user lock and counts the EMIs of the customer's active PBIP loans as
existing debt (the ledger-derived figure only sees repayments already made). Without this, a customer could
"stack" several loans before the first repayment, each passing the 50% test alone; with the user lock, two
concurrent applications are serialised and the second sees the first loan. Once repayments post, they are also
in the ledger-derived figure, so the check over-counts slightly: the safe direction for a lender, stated in the
reasons the customer sees.

### 3. Monthly repayments: a new instruction KIND on the existing engine
The loan's repayments are ONE row in `scheduled_transfers` with `loan_id` set (`frequency = MONTHLY`,
`start_date` = first due date, `end_date` = last due date, `amount` = EMI, `from_account_id` = the loan
account), created by `payments/service.create_loan_repayment_instruction` in the same transaction as the loan.
The Celery Beat job (`execute-scheduled-transfers`, every 60 s) picks it up like any instruction, with the same
per-instruction try-lock, user lock and `FOR UPDATE` re-check, then dispatches on `loan_id`:

* `loans/service.collect_due_instalments` locks the loan, selects every `DUE` instalment with
  `due_date <= today` (`FOR UPDATE`), posts ONE `LOAN_REPAYMENT` for their sum through the ledger, marks them
  `PAID` with the transaction id, closes the loan when none remain, audits and notifies;
* the instruction's `next_run_date` becomes the next unpaid due date, or the instruction is `COMPLETED` when the
  loan closes. Instalment dates are generated with the engine's own month-end clamping (anchored to the first due
  date), so the engine's upcoming-occurrence projection and the schedule agree (unit-tested).

Exactly-once: an instalment can only be paid while `DUE` under its row lock and the loan lock, inside the same
transaction as the posting; a retried or concurrent run finds nothing due and posts nothing.

Not applied to loan repayments: fraud scoring and the daily transfer limit. The money goes to the bank under the
terms the customer accepted when applying, not to a third party; scoring a debit the bank itself initiated could
only BLOCK a contractual repayment. Customers cannot pause or cancel a loan instruction (409); it ends when the
loan is repaid.

### 4. Missed repayments
* **Insufficient funds**: the engine's existing transient-failure path records the failure, audits it and
  notifies the customer; `next_run_date` does not move. Unlike a standing order, a loan instruction is NOT
  skipped by the missed-occurrence recovery: every day after a failure the job tries again to collect ALL
  arrears (overdue + now due) in one posting, until it succeeds. The instalments stay `DUE` and show as overdue in
  `GET /api/v1/loans/{id}`. One failure notification per day of arrears is the reminder.
* **Account frozen/closed** (permanent failure): the instruction is paused `CUSTOMER_ACTION_REQUIRED` as today;
  resuming it moves a past `next_run_date` to today (never skipping it), so arrears are collected on the next tick.
* **Out of scope** (stated, not built): penalty interest or late fees, partial collection, collections
  workflow, credit-bureau reporting, prepayment and foreclosure. A worker outage delays collection; it never
  loses or duplicates an instalment.

### 5. No new service or queue
Applying is one synchronous use case in the existing API (one DB transaction, well under a second). Repayments
reuse the existing Beat job, worker, locks, idempotency and notification outbox. A loan service, a separate
scheduler or a message queue would add a deployable, a second source of truth for due dates and a distributed
transaction between the loan and the ledger, for no capability we need (ADR-0001).

## Alternatives considered
* **A bank-owned loan account per loan, paid by an ordinary instruction (`to_account_id`).** Smallest change to
  the engine, but `_resolve_destination` only accepts the customer's own accounts or a verified payee, so the
  bank account would need a fake payee or an ownership exception; every repayment would also be fraud-scored,
  count against the customer's daily limit and be skipped (not retried) after a miss, and the fixed `amount`
  cannot express the different final instalment. Rejected: it bends three engine rules to fit.
* **A separate loan-repayment Beat job.** No engine change, but a second scheduler with its own locking and
  recovery semantics, and "linked to the scheduled transfers engine" (TASK-047) would not hold.
* **One `scheduled_transfers` row per instalment.** Exact amounts per row, but up to 360 instructions per home
  loan in a table the customer lists and that counts towards their instruction cap.
* **Store `OVERDUE` as a status.** Needs a job to flip it daily and can be stale; deriving it is always right.
* **Disburse into a loan-book (receivable) GL account instead of clearing.** Better accounting, but the
  acceptance criterion names clearing and the receivable is already tracked per loan in `loan_instalments`.

## Consequences
* Good: one transaction creates the loan, its schedule, its repayment instruction and the disbursal; the ledger
  stays the only writer; repayments are exactly-once and retried until paid; no new deployable.
* The clearing account is locked `FOR UPDATE` by every disbursal and repayment, like deposits and external
  transfers already do: a serialisation point to watch if volume grows (then: a loan-book GL account).
* `GET /api/v1/transfers/scheduled` now shows the loan instruction; `ScheduledTransferOut` gains an additive
  `loan_id`. The per-customer instruction cap does not count loan instructions.
* Eligibility over-counts debt slightly after repayments start (conservative, see 2).
* Account-takeover risk: a stolen session could take a loan into the victim's account. Money still leaves only
  through fraud-scored transfers, the recent-lockout rule refuses, and the customer is notified; see threat T41.

## Scope of acceptance
Accepted for the TASK-047/051 pull request only: the tables, postings, the `loan_id` instruction kind and the
missed-repayment behaviour above. Penalty interest, prepayment, loan-book accounting and any human approval step
need their own ADR.
