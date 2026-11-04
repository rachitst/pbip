# ADR 0058: Simulated investing (funds, stocks, bonds), fund SIPs and insurance applications
- Status: Proposed (needs the lead's decision: condition 5 of the owner's standing approval says "no invented
  rates". The prices and the insurance quote factors below are simulated by design, as the task asks, and are
  labelled "simulated" everywhere they are shown)
- Date: 2026-11-04
- Deciders: Architecture Team (section 5 "Wealth Management"; FR-35, FR-36; follow-up to ADR-0025 and ADR-0028)

## Context
Section 5 of the use-case document lists fixed deposits, mutual funds, insurance products, stocks and bonds, and
product comparison. Today only fixed deposits can be booked (ADR-0025). Funds and the gold bond are catalogue rows
"not sold online", insurance is a catalogue with one indicative premium per product (ADR-0028), there are no stocks,
and every recommendation card except three says "You cannot apply for this in PBIP yet".

This adds tables, a system ledger account, two transaction types, a beat job and money-moving endpoints, so
AGENTS.md rule 5 requires this ADR before the code. The ADR-0025 and ADR-0028 "scope of acceptance" sections say
fund or bond purchases and insurance applications need their own ADR; this is it.

## Decision

### 1. Investable instruments: a new reference table seeded by migration `0031`
`investment_instruments` holds what can be bought. Eight rows (fixed ids `50000000-0000-4000-a000-00000000000N`; the
`30000000-...` range is the insurance catalogue's), `catalogue_version = 2026-11-v2`, every one marked
"simulated" in its notes:

| Code | Kind | Risk | Linked catalogue row | Price on 2026-01-01 | Trend a year | Daily deviation up to |
|---|---|---|---|---|---|---|
| `MF-LIQUID` | Mutual fund | LOW | `MF-LIQUID` | INR 1,000.00 | +6.50% | 0.05% |
| `MF-BALANCED` | Mutual fund | MODERATE | `MF-BALANCED` | INR 45.00 | +10.00% | 0.80% |
| `MF-INDEX` | Mutual fund | HIGH | `MF-INDEX` | INR 220.00 | +12.00% | 1.50% |
| `STK-SIMINFRA` | Stock | HIGH | none | INR 845.50 | +11.00% | 2.50% |
| `STK-SIMBANK` | Stock | HIGH | none | INR 1,520.00 | +9.00% | 2.00% |
| `STK-SIMSOFT` | Stock | HIGH | none | INR 3,410.00 | +13.00% | 3.00% |
| `BND-GSEC-7Y` | Bond | LOW | none | INR 1,000.00 | +7.10% | 0.20% |
| `BND-AAA-5Y` | Bond | MODERATE | none | INR 1,000.00 | +7.80% | 0.35% |

* The three funds point at their `wealth_products` row (a real foreign key), so the catalogue and the comparison
  keep one description per fund. The stock companies are fictional (the names say "simulated" and "fictional"),
  so nothing can be read as advice about a listed company.
* **Not investable:** `MF-ELSS` (3-year lock-in) and `SGB` (5-year exit window). A lock-in applies per purchase lot,
  which needs lot-level holdings; that is not built, so they stay catalogue-only, as today. No lock-in is faked.
* The three funds' `wealth_products` notes said "not sold online by PBIP". Migration `0031` replaces that note and
  versions those three rows `2026-11-v2` (its downgrade restores the `0018` note and version), the catalogue API gains
  `investable`, and the assistant's product list says "can be bought online at a simulated price" for them.
* Bonds are **accumulating** in the simulation: the coupon is reflected in the price trend and no coupon is paid
  out. The UI and the notes say so.

### 2. Prices: a deterministic simulated source (`wealth/pricing.py`), never market data
`price(instrument, day)` for an IST bank day, in integer paise per unit:

```
days   = day - 2026-01-01                                       (may be negative)
trend  = base * (1 + drift_bps / 10000 * days / 365)            (exact Fraction)
dev    = (first 8 bytes of SHA-256("<code>|<ISO day>") as an integer) mod (2*wobble_bps + 1) - wobble_bps
price  = round_half_up(trend * (10000 + dev) / 10000), at least 1 paisa
```

Same inputs, same price, in every environment and test; one price per bank day (like a fund NAV), so a quote and a
purchase on the same day agree. It is **not** a market feed: every response carries `price_source = "SIMULATED"`
and the page says "Simulated prices, not market data". No external service is called (ADR condition 4).

### 3. Units: integer micro-units; money: integer paise
`units_micro` is a `BIGINT`, 1 unit = 1,000,000 micro-units (six decimals, finer than the three decimals Indian
fund units are stated in). Never a float or a decimal in storage or JSON.

* **Funds are bought by amount** (`amount_minor`): units = `floor(amount * 1,000,000 / price)`. The customer is
  debited exactly the amount and never receives more units than the amount buys. An amount that buys no unit is 422.
* **Stocks and bonds are bought by whole units** (`units`): amount = `units * price`.
* **Sales** state `units_micro` (stocks and bonds: whole units, so a multiple of 1,000,000). Proceeds =
  `floor(units_micro * price / 1,000,000)`; the bank never pays more than the units are worth. Proceeds of zero
  paise are 422.
* **Cost basis is average cost.** A buy adds its amount; a sale removes `floor(cost * sold / held)` (all of it when
  everything is sold), and the order stores the realised gain (`proceeds - cost removed`). Gain/loss shown on the
  portfolio = value at today's simulated price - remaining cost basis.

### 4. Postings: one system account and two transaction types
Migration `0031` adds `SYS-INVESTMENTS` (`...0000c4`), the settlement counterparty for simulated orders, and widens
`ck_transactions_type_valid`:

| Posting | DEBIT | CREDIT | Type |
|---|---|---|---|
| Purchase (lump sum or SIP) | customer account | `SYS-INVESTMENTS` | `INVESTMENT_PURCHASE` |
| Sale | `SYS-INVESTMENTS` | customer account | `INVESTMENT_SALE` |

New `ledger/service.py` functions `buy_investment` and `sell_investment`; the ledger stays the only writer of
balances and entries. `SYS-INVESTMENTS` is a system account, so a sale at a gain may take it below zero, as the
clearing account does: it stands for the simulated market, which pays the gain. Reconciliation: its balance always
equals `-(sum of sale proceeds) + (sum of purchase amounts)` over the `investment_orders` rows (tested).

Analytics: both types join `NOT_ACTIVITY_TYPES` (never income or spend), like `FD_BOOKING`/`FD_CLOSURE`: the money is
the customer's own, moved into and out of their investments. Without this a purchase would read as spending.

### 5. Holdings and orders (new tables, migration `0031`)
* `investment_holdings`: one row per (customer, instrument), `UNIQUE`, with `units_micro >= 0`,
  `cost_basis_minor >= 0` and `CHECK ((units_micro = 0) = (cost_basis_minor = 0))`. A fully sold holding keeps its
  row at zero (it is hidden from lists).
* `investment_orders`: one immutable row per executed order: side, units, the day's price, amount, cost basis
  moved, realised gain (sales only, `CHECK`), the ledger transaction (`UNIQUE`), the SIP (purchases only, `CHECK`).
  Orders are never updated.

### 6. Lock order and transactions (AGENTS.md section 4, unchanged contract)
**Purchase** `POST /api/v1/wealth/investments/buy` and **sale** `POST /api/v1/wealth/holdings/{id}/sell`, one
use case = one transaction:
1. the per-user advisory lock;
2. the `Idempotency-Key` row (request hash with `"op"`, so a key reused across endpoints is a reuse error);
3. the holding row `FOR UPDATE`: a sale reads it through `wealth/policy.get_owned_holding` (someone else's = 404);
   a purchase first inserts a zero holding `ON CONFLICT DO NOTHING` and then locks it. Purchases and sales take the
   holding lock **before** the accounts, the same order as a fixed-deposit closure takes its deposit row, so two
   sessions of one customer can never wait on each other in opposite orders;
4. the customer account and `SYS-INVESTMENTS` `FOR UPDATE` in sorted id order inside `create_transaction`, BEFORE
   the transaction header and the order row. The account is first read (no lock) through
   `accounts/policy.get_owned_account`, so someone else's account is a 404;
5. holding updated, order inserted, audit (`INVESTMENT_BOUGHT` / `INVESTMENT_SOLD`), outbox notification, stored
   idempotent response, ONE commit.

Insufficient funds raises from the posting; selling more units than held is checked under the holding lock (422).
Either way the transaction rolls back: no posting, no holding change, no order, no key.

### 7. Fund SIPs: a monthly scheduled purchase on the existing beat
* `fund_sips` (customer, user, funding account, fund, amount, `day_of_month` 1-28, `ACTIVE`/`CANCELLED`,
  `next_run_on`) and `fund_sip_runs` (`UNIQUE (sip_id, run_on)`, `EXECUTED` with its order or `FAILED` with a
  reason). Only funds can have an SIP (`MUTUAL_FUND` checked on create).
* `POST /api/v1/wealth/sips` (Idempotency-Key, like creating a scheduled transfer), `GET /api/v1/wealth/sips`,
  `POST /api/v1/wealth/sips/{id}/cancel`; SIPs are read through `wealth/policy` (others' = 404).
* New beat entry `run-fund-sips` -> `pbip.wealth.run_fund_sips`, hourly at minute 50 (away from :05, :20, :35).
  Not folded into `execute-scheduled-transfers`: that would couple `payments` to `wealth` (ADR-0025 section 5,
  ADR-0029 section 1).
* Each due SIP is one use case in its own session and transaction: user lock, the idempotency row
  `fund-sip/{sip_id}/{run_on}` (`/` is outside the header pattern, so no client can pre-claim it), the SIP row
  `FOR UPDATE` re-checked, then exactly the purchase path of section 6 (`channel = SCHEDULED`), the run row, the
  next date, one commit. The permanent guard is `UNIQUE (sip_id, run_on)`.
* **Failure rule:** insufficient funds or an inactive account rolls the purchase back; a second transaction records
  a `FAILED` run with the reason, notifies the customer and moves to the next month. A missed run is not retried
  and missed months are not caught up (real SIPs skip a failed instalment). `next_run_on` is always the first
  scheduled day after the run's bank day.

### 8. Insurance: a simulated quote and an application record, no underwriting, no premium debit
* `POST /api/v1/wealth/insurance/quote` and `POST /api/v1/wealth/insurance/applications` take the product, cover,
  term, the customer's **declared age band** and `age_band_consent: true`. The band is declared for this quote and
  stored only on the application; it is not read from the date of birth, because the ADR-0015 consent is for the
  purpose "product_recommendations" and purpose limitation forbids reusing it for pricing.
* **Quote rule `IQ-1`** (simulated, stated on the page): the product's indicative premium scaled linearly by
  `cover / reference cover`, by an age-band factor relative to the reference age's band (18-25: 0.80, 26-35: 1.00,
  36-50: 1.60, 51-59: 2.60, 60+: 4.00) and, for term life only, by `1 + 1.5% x (term - reference term)` (at least
  0.50); rounded half up to whole paise. It is not an insurer's price.
* **Status rules (deterministic, in order):** `DECLINED` if no age in the declared band is inside the entry ages, or
  (term life) the youngest eligible age plus the term passes the maximum cover age. `SUBMITTED` (referred: an insurer
  would need the exact age or medicals) if only part of the band is eligible, the oldest eligible age plus the term
  passes the maximum cover age, or the cover is more than twice the reference cover. Otherwise
  `APPROVED_SIMULATED`. A referred application stays `SUBMITTED`: PBIP has no insurer partner to decide it, and the
  page says so. The reason is stored and shown.
* **No premium is debited.** No policy is issued, so collecting a premium would take money for nothing; the
  application says "no policy is issued and nothing is charged". Applications are audited
  (`INSURANCE_APPLICATION_SUBMITTED`) and idempotent.

### 9. Comparison by risk band
`GET /api/v1/wealth/suitability` returns the customer's RTQ-1 band and, for every catalogue product and
instrument, `suitable` and a reason by the rule the assistant already uses (TASK-105): a product is suitable when
its risk level is at or below the band (LOW < MODERATE < HIGH). Insurance is protection and is not risk-rated
(`suitable = null`, reason says so). With no band recorded, `band = null` and every `suitable` is null with "No
risk profile recorded". The buy flow shows the same warning; it does not block a purchase (the customer may still
choose, as with any execution-only order).

### 10. Recommendation cards
Every card gets an action chosen by `product_id` and `product_type` only: loans go to `/loans?product=<id>`
(preselected when the id is a catalogue loan), the fixed deposit to the booking dialog, investments to
`/wealth/invest` (preselected when the id is an instrument), insurance to `/wealth/insurance` (preselected when the
id is an insurance product), the credit line to `/loans`. **The rewards card** has no flow: PBIP issues no cards (no
card network, no card ledger), and an "apply" button that records nothing would be a fake flow (AGENTS.md rule 7).
Its card keeps a plain "PBIP does not issue cards yet" note.

### 11. Fraud scoring: investing debits are not fraud-scored
A purchase moves money from the customer's own account to the bank's settlement account and back on sale; no
money can leave the bank to a third party through it, which is what the scorer protects (transfers and bill
payments to payees, `SCORED_PAYMENT_TYPES`). The same holds for `FD_BOOKING` (ADR-0025). The sale proceeds go only
to one of the customer's own accounts. `ACTIVITY_TYPES` (dormancy) is **not** widened: an investment does not wake a
dormant account for `DORMANT_REACTIVATION`, the conservative side. No fraud decision changes.

## Alternatives considered
* **Buy funds through `wealth_products`.** It would need price columns on deposits and the gold bond, and its
  `bookable` CHECK is deliberately fixed-deposit-only; a separate instrument table keeps both strict.
* **A random-walk price stored per day.** Realistic, but needs a writer job and stored data that differs by
  environment; a pure function is reproducible and testable.
* **Lot-level holdings** for ELSS and the gold bond. Correct for lock-ins, but a larger change; deferred.
* **Credit purchases to `SYS-CLEARING`.** One account fewer, but investments could not be reconciled separately.
* **Hold instead of posting** (as for a pending review). The money leaves the account until the customer sells.
* **Debit the first insurance premium.** Rejected in section 8: nothing is issued.
* **Reuse the recommendations consent for the insurance age band.** Rejected: different purpose (ADR-0015).

## Consequences
* Good: every rupee moves through the ledger in the standard lock order; purchases, sales and SIP runs are
  exactly-once per key; holdings, orders and the settlement account reconcile.
* `SYS-INVESTMENTS` is locked by every order (a serialisation point, as `SYS-FIXED-DEPOSITS` is).
* A stolen session can buy or sell the victim's investments, but money only moves between the victim's own accounts
  and the bank (threat T71).
* Prices and quotes are simulated; the UI says so on every screen that shows one.

## Scope of acceptance
The instrument table and seed, the price function, the two postings, holdings, orders, SIPs, insurance applications
and the suitability endpoint above. Lot-level lock-ins (ELSS, gold bond), coupons, dividends, tax, nominees and a
real premium need their own ADR.
