# State diagrams

Four lifecycles, with the statuses taken from the enums and the transitions from the services that set them. Paths are
relative to `apps/api/src/pbip_api/`. The [LLD section 4](../architecture/LLD.md#4-state-machines) has the
transition tables with the proving tests, plus the hold, bill payment and scheduled transfer machines.

| Diagram | Enum | Transitions |
|---|---|---|
| [1. Transaction](#1-transaction-lifecycle) | `TransactionStatus` in `modules/ledger/models.py` | `modules/payments/service.py`, `payments/step_up_expiry.py`, `modules/bills/service.py`, `modules/ledger/service.py` |
| [2. Fraud alert](#2-fraud-alert-lifecycle) | `AlertStatus`, `AlertSeverity` in `modules/fraud/models.py` | `modules/fraud/service.py` (`open_alert`), `fraud/structuring_monitor.py`, `PaymentsService.review_alert` |
| [3. Loan](#3-loan-lifecycle) | `LoanStatus`, `InstalmentStatus` in `modules/loans/models.py` | `modules/loans/service.py` |
| [4. Fixed deposit](#4-fixed-deposit-lifecycle) | `FixedDepositStatus`, `ClosureKind` in `modules/wealth/models.py` | `modules/wealth/service.py`, `modules/wealth/maturity_service.py` |

## 1. Transaction lifecycle

Exports: [SVG](svg/state-transaction.svg), [PNG](png/state-transaction.png).
`TransactionStatus` has six values. `INITIATED` exists only inside the database transaction that creates the header:
any failure before the commit rolls the header back, so no `INITIATED` row is ever visible. The same machine serves
transfers, scheduled occurrences (where `STEP_UP` becomes `PENDING_REVIEW`) and bill payments (which have no step-up
flow). Ledger-internal types (deposits, loan disbursals and repayments, deposit bookings and payouts) are posted
directly: `INITIATED` to `POSTED` in one commit.

<!-- diagram: state-transaction -->
```mermaid
stateDiagram-v2
    [*] --> INITIATED: ledger.create_transaction, accounts locked
    INITIATED --> POSTED: ALLOW, daily limit ok, entries posted
    INITIATED --> PENDING_STEP_UP: STEP_UP, interactive, MFA enabled
    INITIATED --> PENDING_REVIEW: REVIEW, or STEP_UP nobody can answer, hold placed, alert opened
    INITIATED --> DECLINED: BLOCK, decline_reason FRAUD_BLOCK, alert opened
    PENDING_STEP_UP --> POSTED: valid TOTP code, entries posted
    PENDING_STEP_UP --> PENDING_REVIEW: third wrong code, hold placed, alert opened
    PENDING_STEP_UP --> DECLINED: unanswered past STEP_UP_EXPIRY_MINUTES, STEP_UP_EXPIRED
    PENDING_REVIEW --> POSTED: analyst RELEASE, hold captured
    PENDING_REVIEW --> DECLINED: analyst REJECT, hold released, ANALYST_REJECTED
    POSTED --> REVERSED: ledger.reverse_transaction, compensating REVERSAL
    POSTED --> [*]
    DECLINED --> [*]
    REVERSED --> [*]
    note right of REVERSED
        reverse_transaction is a ledger function with
        tests but no HTTP route, job or script on main.
    end note
```

## 2. Fraud alert lifecycle

Exports: [SVG](svg/state-fraud-alert.svg), [PNG](png/state-fraud-alert.png).
An alert has three statuses and two severities. It is opened in the same database transaction as its cause and
resolved once; a second decision is a 409. The decision changes the transaction only when the transaction is
`PENDING_REVIEW`; on a declined (engine `BLOCK`) or already posted (structuring monitor) transaction it is a label.

<!-- diagram: state-fraud-alert -->
```mermaid
stateDiagram-v2
    [*] --> OPEN: REVIEW or BLOCK decision, third wrong step-up code, or structuring monitor
    state OPEN {
        [*] --> MEDIUM: risk_score below 85
        [*] --> HIGH: risk_score 85 or more
    }
    OPEN --> FALSE_POSITIVE: analyst RELEASE, transaction posted if held
    OPEN --> CONFIRMED_FRAUD: analyst REJECT, transaction declined if held
    FALSE_POSITIVE --> [*]
    CONFIRMED_FRAUD --> [*]
    note right of OPEN
        Resolution stores the analyst, the note and
        resolved_at, and audits FRAUD_ALERT_RESOLVED.
        There is no separate assign or claim step.
    end note
```

## 3. Loan lifecycle

Exports: [SVG](svg/state-loan.svg), [PNG](png/state-loan.png).
A loan exists only once it is disbursed: there is no application, pending-approval or rejected state, because the
application either passes the eligibility check and disburses in the same commit or is refused with a 422 and leaves
nothing behind (ADR-0024). "Overdue" is computed on read (`DUE` with a due date before today) and never stored.

<!-- diagram: state-loan -->
```mermaid
stateDiagram-v2
    state "Loan (loans.status)" as Loan {
        [*] --> ACTIVE: POST /loans/apply eligible, LOAN_DISBURSAL posted, schedule booked
        ACTIVE --> ACTIVE: repayment collected, DUE instalments left
        ACTIVE --> CLOSED: last DUE instalment paid
        CLOSED --> [*]
    }
    state "Instalment (loan_instalments.status)" as Instalment {
        [*] --> DUE: booked at apply
        DUE --> PAID: LOAN_REPAYMENT posted by the scheduler
        PAID --> [*]
    }
    note right of Loan
        The repayment instruction (scheduled_transfers with loan_id)
        becomes COMPLETED with the loan. Insufficient funds keep the
        instalments DUE and retry them as arrears. An inactive account
        pauses the instruction. No default, write-off or prepayment state.
    end note
```

## 4. Fixed deposit lifecycle

Exports: [SVG](svg/state-fixed-deposit.svg), [PNG](png/state-fixed-deposit.png).
The stored status is `ACTIVE` or `CLOSED`, with `closure_kind` `MATURED` or `PREMATURE` once closed. The API also
reports `MATURED` for an `ACTIVE` deposit on or after its maturity date; that state is derived on read. A held payout
is an `ACTIVE` deposit with `payout_held_at` set.

<!-- diagram: state-fixed-deposit -->
```mermaid
stateDiagram-v2
    state "ACTIVE" as Active
    state "ACTIVE, shown as MATURED (derived on read)" as Matured
    state "ACTIVE with payout_held_at (account not active)" as Held
    state "CLOSED, closure_kind PREMATURE" as ClosedPremature
    state "CLOSED, closure_kind MATURED" as ClosedMatured

    [*] --> Active: POST /wealth/fixed-deposits/book, FD_BOOKING posted
    Active --> ClosedPremature: customer close after the lock-in, penalty rate, FD_CLOSURE and FD_INTEREST posted
    Active --> Matured: bank date reaches the maturity date
    Matured --> ClosedMatured: hourly payout job, or customer close, full maturity amount posted
    Matured --> Held: payout job finds the account frozen or closed, notified once
    Held --> ClosedMatured: account active again, next hourly run pays out
    ClosedPremature --> [*]
    ClosedMatured --> [*]
```

**Extension point.** A new lifecycle gets its own numbered section with a `<!-- diagram: state-... -->` marker, its
enum and service in the table at the top, and an entry in [the index](README.md). A new value in an existing enum is a
new state here and a new row in the LLD transition table.
