# ADR 0011: Monthly spending aggregates are materialised nightly; the request path never writes
- Status: Accepted
- Date: 2026-10-29
- Deciders: Architecture Team

## Context
TASK-040 adds `GET /api/v1/analytics/spending` and `/trends`, and requires that "queries leverage
pre-aggregated monthly tables" (Blueprint: "materialised monthly aggregates via Celery"). The first
implementation filled `monthly_category_spending` lazily from the GET handler and committed inside
it: a write on a read path, a mid-use-case commit, a unique-key race between two first readers
(an `IntegrityError`, hence a 500), and rows that could never be refreshed. It also counted every
DEBIT entry as spend, including reversed transfers and the counterparty's REVERSAL debit.

## Decision
* **Spend** of an account-month is the sum of DEBIT entries of POSTED transactions that are not
  of type REVERSAL. A reversed transfer (status REVERSED) was refunded, so it is not spend; the
  REVERSAL that debits the counterparty is money being returned. Deposits and incoming transfers
  are CREDIT entries and never count. Months are UTC calendar months on the entry timestamp.
* **Write path:** the Celery beat task `pbip.analytics.materialize_monthly_spending` (02:30 UTC)
  recomputes the two most recent closed months for every customer account with debits and
  upserts the rows under the unique key `(account, year, month, category)`, deleting categories
  that vanished. It is idempotent and commits once. The second month is refreshed because a
  transfer can be reversed weeks after it was posted.
* **Read path:** a closed month is served from the stored rows when they exist; otherwise, and
  always for the running month, it is computed from the ledger entries. The request path never
  writes.
* **Percentages:** a change relative to a previous value of zero is `null`, never "+100%".

## Alternatives considered
- Keep the lazy fill on GET with `ON CONFLICT`: removes the race but keeps a write and a commit
  inside a read use case, and rows still go stale after a reversal.
- Update the aggregate from the ledger service on every posting/reversal: couples the ledger to
  analytics and adds work to the money path; rejected (the ledger stays the only writer of ledger
  tables and knows nothing about categories).
- Net refunds into the month they happen (immutable past months, no refresh needed): produces
  negative category amounts and percentages; rejected for a customer-facing breakdown.

## Consequences
- A reversal older than two months is not reflected in the stored rows until a manual
  re-materialisation (`materialize_monthly_spending(session, periods=[...])`); documented limit.
- Reads of the running month scan that month's entries for one account (bounded by the account's
  monthly debit volume, indexed by account); closed months are one indexed lookup.
