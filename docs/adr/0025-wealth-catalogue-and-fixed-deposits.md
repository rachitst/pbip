# ADR 0025: Wealth catalogue, fixed-deposit booking and closure through the ledger
- Status: Accepted (for the scope of TASK-063 / TASK-064 / TASK-065 only; see "Scope of acceptance")
- Date: 2026-11-02
- Deciders: Architecture Team (TASK-063 / #86, TASK-064 / #87, TASK-065 / #88)

## Context
The requirements list "Wealth Management: fixed deposits, mutual funds, insurance products, stocks and bonds,
product comparison", and UC-07 needs investment products to recommend. `modules/wealth/` was an empty package.
The three tasks ask for:

1. TASK-063: `GET /api/v1/wealth/products` returning fixed deposit, mutual fund and sovereign gold bond specs
   with tenure options, annualised yield (APY) and minimum lock-in, filterable by `risk_level` (LOW, MODERATE,
   HIGH). Definition of done: "Alembic migration for wealth catalogue".
2. TASK-064: `POST /api/v1/wealth/fixed-deposits/book` reserves the principal through a ledger entry, creates a
   `fixed_deposit` record with maturity date and projected interest, requires `Idempotency-Key`, validates the
   source balance and emits a confirmation notification through the outbox. Definition of done: integration tests
   of ledger balancing AND premature closure calculations.
3. TASK-065: a portfolio page with invested capital, accrued returns, active deposits with a maturity countdown,
   and an asset allocation chart (Cash, Deposits, Funds).

This adds tables, a system ledger account, three transaction types and two money-moving endpoints, so AGENTS.md
rule 5 requires this ADR before code. Product-owner rule: products and rates come from a real, versioned
catalogue, never invented per request; rates are stated as indicative and simulated.

## Decision

### 1. The catalogue is reference data seeded by migration `0017` (table `wealth_products`)
Unlike the loan catalogue (code, ADR-0024), the wealth catalogue is a table, because the definition of done asks
for a catalogue migration, TASK-096 (insurance) asks for catalogue data "seeded through a migration and the same
in every environment", and a booked deposit can then reference its product with a real foreign key. Each row
carries `catalogue_version` (`2026-11-v1`); a rate change is a new migration (reviewed, reversible, identical in
every environment), never an edit in place, and a booked deposit keeps a snapshot of the terms it was booked on.

Eight products, all marked simulated in their notes:

| Code | Type | Risk | Yield basis | Rate | Tenures (months) | Lock-in | Bookable |
|---|---|---|---|---|---|---|---|
| `FD-SHORT` | Fixed deposit | LOW | Guaranteed | 6.50% | 6, 9 | 0 | yes |
| `FD-REGULAR` | Fixed deposit | LOW | Guaranteed | 7.10% | 12, 18, 24, 36 | 0 | yes |
| `FD-TAX-SAVER` | Fixed deposit | LOW | Guaranteed | 6.75% | 60 | 60 (no premature closure) | yes |
| `MF-LIQUID` | Mutual fund | LOW | Market-linked | none | open-ended | 0 | no |
| `MF-BALANCED` | Mutual fund | MODERATE | Market-linked | none | open-ended | 0 | no |
| `MF-INDEX` | Mutual fund | HIGH | Market-linked | none | open-ended | 0 | no |
| `MF-ELSS` | Mutual fund | HIGH | Market-linked | none | open-ended | 36 | no |
| `SGB` | Sovereign gold bond | MODERATE | Fixed coupon | 2.50% coupon | 96 | 60 | no |

* Fixed-deposit rates are PBIP's own indicative card rates, of the order of Indian bank FD rates in 2026 but not
  copied from any bank. The tax-saver deposit's 5-year lock-in and INR 1.5 lakh cap, the ELSS 3-year lock-in and the
  gold bond's 8-year tenure, 5-year exit window and 2.50% coupon are the published product rules of those Indian
  product types.
* **Market-linked products show no yield.** PBIP has no market-data feed, so it states no APY, NAV or past
  performance for funds (`apy_bps = null`, `yield_basis = MARKET_LINKED`) rather than inventing one. The gold
  bond's APY is its fixed coupon; the note says the gold price is not shown and returns also depend on it.
* Only fixed deposits are `bookable`. Funds and gold bonds are listed for discovery and for UC-07/TASK-098
  comparison; buying them would need fund-house/RBI integrations the simulation does not have.
* APY is derived, not stored: for a deposit compounded quarterly, `APY = (1 + r/4)^4 - 1`, computed with
  `Fraction` and rounded half up to whole basis points.

### 2. Fixed-deposit maths: exact `Fraction`, integer paise (`wealth/deposit_math.py`)
Cumulative deposit, interest compounded **quarterly** (the Indian bank convention), every tenure a multiple of 3
months so a deposit is a whole number of quarters. With `r` the annual rate:
* **maturity amount** = `P (1 + r/4)^q`, `q = tenure_months / 3`, rounded half up once to whole paise; projected
  interest = maturity amount - principal. Maturity date = start date + tenure in calendar months, clamped to the
  month's last day (31 Aug + 6 months = 28/29 Feb). The start date is the bank day (IST) of booking.
* **accrued interest on a date** = `P (1 + r/4)^k (1 + r d / 365) - P`, where `k` is the number of completed
  quarters since the start (calendar quarters, same clamping) and `d` the days since the last completed quarter
  (actual/365). On the maturity date it equals the projected interest exactly; it never exceeds it.
* **premature closure** pays the principal plus interest accrued at the contract rate **minus a 1.00% penalty**
  (floored at 0%) for the time actually held; a deposit closed within 7 days of booking earns no interest (RBI
  practice). A deposit inside its lock-in (the tax-saver) cannot be closed early: 422.

### 3. Postings: a dedicated system account and three transaction types
Migration `0017` adds the system account `SYS-FIXED-DEPOSITS` (`...0000c3`), the bank's liability for money held
on deposit, and widens `ck_transactions_type_valid`:

| Posting | DEBIT | CREDIT | Type |
|---|---|---|---|
| Booking | customer account | `SYS-FIXED-DEPOSITS` | `FD_BOOKING` |
| Closure (principal back) | `SYS-FIXED-DEPOSITS` | customer account | `FD_CLOSURE` |
| Interest (only if > 0) | `SYS-CLEARING` | customer account | `FD_INTEREST` |

New `ledger/service.py` functions `book_fixed_deposit` and `pay_out_fixed_deposit`; the ledger stays the only
writer. The principal therefore sits on its own account, so the `SYS-FIXED-DEPOSITS` balance = the principal of
every open deposit (reconciliation check, tested). Interest is the bank's expense, paid from clearing like loan
disbursals. "Reserves principal via ledger clearing entry" is met by a real posting rather than a `hold`: a hold
only reserves funds pending a decision and would leave the money on the customer's balance for months.

Analytics: the money is still the customer's, so `FD_BOOKING` and `FD_CLOSURE` are excluded from income and spend
(like a move between own accounts); without this, booking a deposit would read as spending and depress the health
score. `FD_INTEREST` is not counted as income (it is not in `INCOME_TYPES`), the conservative side for loan
eligibility.

### 4. Lock order and transactions (AGENTS.md section 4, unchanged contract)
**Booking**: (1) per-user advisory lock, (2) `idempotency_keys` insert, (3) owned account via
`accounts/policy.get_owned_account`, product and amount checks (reads), (4) customer account and
`SYS-FIXED-DEPOSITS` `FOR UPDATE` in sorted id order inside `create_transaction`, BEFORE the transaction header and
the `fixed_deposits` row (their FKs take KEY SHARE locks), (5) audit `FIXED_DEPOSIT_BOOKED`, outbox
`FIXED_DEPOSIT_BOOKED`, stored idempotent response, ONE commit. Insufficient funds raises from the posting and
rolls everything back (422, no partial state).

**Closure** (`POST /api/v1/wealth/fixed-deposits/{id}/close`): (1) user lock, (2) idempotency key, (3) the
deposit row `FOR UPDATE` through `wealth/policy.get_owned_fixed_deposit` (404 for others), refuse if already
closed (409) or locked in (422), (4) `pay_out_fixed_deposit` locks ALL THREE accounts (customer, deposits,
clearing) in sorted order before either header, (5) deposit row updated, audit, outbox, response, one commit.

### 5. Maturity: computed on read, paid out when the customer closes it (no new background job)
Status `MATURED` is derived (`ACTIVE` and today >= maturity date), like "overdue" in ADR-0024, so it can never be
stale. A matured deposit is paid out by the same closure use case, at exactly the projected maturity amount; it
earns nothing after maturity (stated in the UI). We do not add a Beat job: automatic payout or auto-renewal is
an instruction the customer would have to choose at booking (payout account, renew or not), which the
acceptance criteria do not ask for, and a job would add a second money-moving path with its own retry semantics.
Reusing `execute-scheduled-transfers` would couple `payments` to `wealth`. Automatic maturity payout is follow-up
work with its own ADR (see "Scope of acceptance").

### 6. Portfolio is computed on read
`GET /api/v1/wealth/portfolio`: cash = ledger balance of the customer's INR accounts; deposits = principal of
open deposits + interest accrued to today (section 2); funds = 0, because PBIP holds no fund units for anyone
(the field says so); allocation shares in basis points that sum to 10,000 (largest remainder). No stored totals.

## Alternatives considered
* **Catalogue in code** (as loans). Simpler, but fails the catalogue-migration definition of done and the
  TASK-096 "seeded through a migration" rule, and gives the deposit no FK to its product.
* **Place a hold instead of posting.** The money would stay on the customer's ledger balance for the whole term
  and holds are for short-lived review decisions; a deposit is a contract that moves the money.
* **Credit the deposit to `SYS-CLEARING`.** One account fewer, but mixes customer deposits with every external
  transfer and loan, and the bank's deposit liability could not be reconciled.
* **One three-leg closure transaction.** Fewer rows, but the `transactions` header has one debit and one credit
  account; two two-leg postings keep every existing reader (statements, analytics) correct and show principal
  and interest separately, as bank statements do.
* **A nightly maturity job.** See section 5.
* **Simple interest.** Easier, but Indian bank deposits compound quarterly; customers compare the APY.

## Consequences
* Good: one transaction books the deposit and moves the money; exactly one posting per idempotency key;
  closure is exactly-once under the row lock; no new deployable or job.
* `SYS-FIXED-DEPOSITS` and `SYS-CLEARING` are locked `FOR UPDATE` by every booking/closure (a serialisation point,
  like deposits and loans already are).
* A matured deposit waits for the customer; the page shows it as "Matured, ready to withdraw".
* Account-takeover risk: a stolen session can book or close the victim's deposits, but money only moves between
  the victim's own account and the bank; see threat T45.
* Rates are simulated; the UI says "indicative, simulated rates".

## Scope of acceptance
Accepted for the TASK-063/064/065 pull request: the catalogue table and seed, the postings, the closure rules and
the computed-on-read maturity above. Fund or bond purchases, auto-renewal, automatic maturity payout, TDS (tax
deducted at source) and nominee handling need their own ADR.
