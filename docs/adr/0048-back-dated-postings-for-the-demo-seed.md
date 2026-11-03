# ADR 0048: Back-dated postings for the demo seed, never before an account's history
- Status: Accepted (under owner's standing approval, 2026-11-04)
- Date: 2026-11-04
- Deciders: Architecture Team (follow-up to ADR-0043)

## Context
ADR-0043 made income a credit source that recurs in at least 2 of the last 3 calendar months. The demo seed
(`scripts/seed_dev.py`) funded each customer with one opening deposit, so Alice, Bob and Carol had no income: the
loan page said "We could not find regular income", the assistant asked for an amount instead of answering "can I
afford a 5 lakh car loan over 5 years?", and income-based recommendations disappeared. A salary history needs
credits dated in earlier months, but the ledger dated every posting "now", and AGENTS.md section 4 lets only
`ledger/service.py` insert entries. The only existing way to date a posting was the test helper
`pbip_test_support._redate_transaction`, which disables the entries' immutability trigger and rewrites timestamps
after the fact: acceptable for a throw-away test database, not for a script that runs against a shared one.

An entry's `balance_after` is the account's running balance when it was posted, and statements
(`statements/service.build_statement`: opening = first line's `balance_after` - credit + debit), daily balances and
the balance forecast read those values in time order. Entries are immutable, so a credit dated before an existing
entry would leave every later `balance_after` permanently wrong.

## Decision
`ledger.deposit` takes an optional, keyword-only `posted_at`. When given, the ledger locks the clearing and target
accounts in sorted id order (the standard lock order, before the header is inserted) and refuses it with
`LedgerInvariantError`, before anything is written, when:
- the environment is staging or production (`Settings.is_deployed`);
- it is naive (not timezone-aware) or in the future;
- it is earlier than the target account's latest entry (equal is allowed), so `balance_after` stays the running
  balance in time order.

The date goes on the transaction header (`created_at` and `posted_at`) and on both entries (`created_at`).
`ledger.seed_payment` is the matching debit for the seed's spending history: a posted `TRANSFER` from the customer
account to the clearing account (the simulated external rail, as for any live transfer to another bank's payee),
with a payee, a mandatory `posted_at`, the same guards and the usual balance check (a payment the account cannot
afford is refused). Seeded history is **not fraud-scored** and has no `fraud_events` row: scoring decides about a
payment being made now, and `payments/service.py` stays the only path for those. Later live transfers read the
seeded history like any other (90-day average, 30-day debits).
`create_transaction` and `post_transaction` gain a matching keyword-only `at`, which only `deposit` passes. Nothing
else changes: double entry, the balance trigger, the immutability trigger and the CHECK constraints all apply as
before. The clearing account is not checked: it is a system account whose running balance is never shown by date.

The seed credits each demo customer a monthly salary (Alice INR 1,25,000, Bob INR 60,000, Carol INR 90,000) at
10:00 IST on the 1st of the running month and the two before it, reference "Salary <Month YYYY>", and dates a new
customer's opening deposit 09:00 IST on the 1st of the oldest month, so the monthly income is exactly the salary.
Alice also gets 18 payments a month up to yesterday (rent, a home-loan EMI, groceries, utilities, dining,
transport, shopping, entertainment, healthcare; references that `analytics/categorization.py` recognises) to 16
seeded, verified external payees, and the verified internal payee "Bob Demo". Her average payment is about
INR 3,000-3,700 (by day of the month), so the fraud demo's amounts stay 5x or more of it. Bob and Carol get the
salary only: they are the fraud lab's senders, and a payment history would change their `AMOUNT_SPIKE` and model
inputs. Each customer's history is one database transaction (all or nothing); a line already posted is skipped
(re-runs in the same month add nothing). A customer whose account already has entries after the history's dates
(a database seeded before this change and used since) is skipped with a message: their history can only be built
on a database seeded from scratch.

## Alternatives considered
- **Reuse `_redate_transaction` in the seed.** Disables the immutability trigger on a shared database and rewrites
  posted rows; it would also put a second writer of entries outside `ledger/service.py`.
- **Allow any past date.** Works on every existing database, but each back-dated credit falsifies the running
  balance of every later entry forever (statements would show a wrong opening balance and a jumping balance
  column; the forecast would read a false series).
- **A separate value date column.** The banking-correct model (booking date vs value date), but a schema change
  for every query that reads `created_at` as the date, for a demo seed.
- **Seed with today's date only and wait two months.** No income for the demo until then.
- **Post the seed's payments through `PaymentService` (fraud-scored).** Scoring is a decision about a payment made
  now; history scored at seed time would open alerts and holds, and could not be dated.

## Consequences
- **Customer-visible (demo data only):** seeded customers have three salary credits and an income equal to their
  salary. Alice has three months of spending, so the spending breakdown, the forecast and recent transactions
  have data, and "can I afford a 5 lakh car loan over 5 years?" is answered "likely to be eligible" (with her EMI,
  repayments 18% of income). No API, schema, fraud rule, model or customer-facing behaviour changes.
- **Fraud decisions:** no rule or model changes, but Alice's inputs do: her 90-day average transfer is about
  INR 3,700 (on the 4th of a month), so `AMOUNT_SPIKE` fires from about INR 18,600. Her payees, including
  "Bob Demo", are created when the seed runs, so for 24 hours a large transfer to any of them adds
  `NEW_PAYEE_LARGE_AMOUNT`. Bob and Carol have no payment history, as before. `DEPOSIT` credits are not inbound
  money for the mule rule (ADR-0035).
- Seeded payees keep their real `created_at`, so they are younger than their own payments; only payee-age rules
  read that column.
- A seeded account's rows keep their real `created_at`, so its first entries can predate it; only ordering reads
  that column.
- An existing demo database whose demo customers have transacted since being seeded cannot be given a salary
  history; the seed says so per customer and posts nothing for them.
- Tests: `test_ledger_backdating.py` (header and entries dated, balanced, immutable, every refusal leaves nothing,
  concurrent back-dated deposits, seed payments to clearing and their refusals), `test_dev_scripts.py` (income
  equals salary, running balances true, history in date order and never negative, categories, average payment,
  unscored, Bob and Carol without payments, the assistant's answer, idempotent re-run, the skip on an older
  database, no ledger writes in the seed).

Standing approval holds: (1) no security, auth, RBAC, BOLA or audit change, and the capability is refused in
staging/production with no API exposure; (2) money moves only through `ledger/service.py`, in the standard lock
order, concurrency-tested; the seed is idempotent; (3) no migration; (4) no new service, dependency or outbound
data; (5) the data is labelled demo data from the seed and is not presented as a real customer's; (6) within the
demo-readiness scope of TASK-089 and FR-30/FR-38.
