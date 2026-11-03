# ADR 0048: Back-dated deposits for the demo seed, never before an account's history
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
`create_transaction` and `post_transaction` gain a matching keyword-only `at`, which only `deposit` passes. Nothing
else changes: double entry, the balance trigger, the immutability trigger and the CHECK constraints all apply as
before. The clearing account is not checked: it is a system account whose running balance is never shown by date.

The seed credits each demo customer a monthly salary (Alice INR 1,25,000, Bob INR 60,000, Carol INR 90,000) at
10:00 IST on the 1st of the running month and the two before it, reference "Salary <Month YYYY>", and dates a new
customer's opening deposit 09:00 IST on the 1st of the oldest month, so the monthly income is exactly the salary. A
salary already credited is skipped (re-runs in the same month add nothing). A customer whose account already has
entries after the salary dates (a database seeded before this change and used since) is skipped with a message:
their history can only be built on a database seeded from scratch.

## Alternatives considered
- **Reuse `_redate_transaction` in the seed.** Disables the immutability trigger on a shared database and rewrites
  posted rows; it would also put a second writer of entries outside `ledger/service.py`.
- **Allow any past date.** Works on every existing database, but each back-dated credit falsifies the running
  balance of every later entry forever (statements would show a wrong opening balance and a jumping balance
  column; the forecast would read a false series).
- **A separate value date column.** The banking-correct model (booking date vs value date), but a schema change
  for every query that reads `created_at` as the date, for a demo seed.
- **Seed with today's date only and wait two months.** No income for the demo until then.

## Consequences
- **Customer-visible (demo data only):** seeded customers have three salary credits and an income equal to their
  salary; Alice's "can I afford a 5 lakh car loan over 5 years?" is answered "likely to be eligible" (EMI about 8%
  of income). No API, schema, fraud rule, model or customer-facing behaviour changes.
- **Fraud decisions:** unchanged. Fraud features read debits; `DEPOSIT` credits are not inbound money for the mule
  rule (ADR-0035). Seeded customers have a larger balance.
- A seeded account's rows keep their real `created_at`, so its first entries can predate it; only ordering reads
  that column.
- An existing demo database whose demo customers have transacted since being seeded cannot be given a salary
  history; the seed says so per customer and posts nothing for them.
- Tests: `test_ledger_backdating.py` (header and entries dated, balanced, immutable, every refusal leaves nothing,
  concurrent back-dated deposits), `test_dev_scripts.py` (income equals salary, running balances true, the
  assistant's answer, idempotent re-run, the skip on an older database, no ledger writes in the seed).

Standing approval holds: (1) no security, auth, RBAC, BOLA or audit change, and the capability is refused in
staging/production with no API exposure; (2) money moves only through `ledger/service.py`, in the standard lock
order, concurrency-tested; the seed is idempotent; (3) no migration; (4) no new service, dependency or outbound
data; (5) the data is labelled demo data from the seed and is not presented as a real customer's; (6) within the
demo-readiness scope of TASK-089 and FR-30/FR-38.
