# ADR 0028: Insurance catalogue as migration-seeded reference data, with no underwriting
- Status: Accepted (product owner, 2026-11-03: pre-approved on four conditions, all met: seeded fixtures labelled illustrative and simulated, not insurer quotes; no purchase or payment flow; no customer PII in the table; working downgrade on the linear migration chain)
- Date: 2026-11-02
- Deciders: Architecture Team (TASK-096 / #179, TASK-098 / #181)

## Context
The requirements list "insurance products" in the wealth scope (FR-35) and "product comparison" (FR-36). UC-06
(wealth) and UC-07 (recommendations) need insurance to be part of the same catalogue the customer can browse and
compare. The tasks ask for:

1. TASK-096: `GET /api/v1/wealth/insurance` returning term-life and health products with cover range and an
   indicative premium in minor units, eligibility hints (age band, cover limits) **without performing any
   underwriting**, and catalogue data "seeded through a migration and the same in every environment".
2. TASK-098: a comparison of two to four products (wealth and insurance) side by side.

ADR-0025 created `wealth_products` (deposits, funds, gold bond), seeded by migration `0018`. Adding insurance means
a new table (AGENTS.md rule 5, and the product-owner rule that an ADR comes before any new table).

## Decision

### 1. A separate table `insurance_products`, not rows in `wealth_products`
Insurance is protection, not investment. Its facts are different: a cover range (sum assured), entry ages, a
maximum cover age, policy terms in years, and a premium that the customer **pays** every year. A wealth product has
an amount the customer **invests**, a yield, a yield basis, a lock-in, a risk level and a premature-closure
penalty. Putting insurance in `wealth_products` with a `product_type` would need:

* about eight new nullable columns that are meaningless for every deposit and fund, and the eight wealth columns
  (`yield_basis`, `annual_rate_bps`, `min_lock_in_months`, `premature_penalty_bps`, `bookable`, ...) left null or
  given invented values for insurance;
* every CHECK constraint rewritten as "if insurance then ... else ...", which is how silent nonsense rows get in;
* `risk_level` on insurance, which would be false: a term plan has no market risk, and showing it as "LOW risk"
  next to a fund would mislead the comparison.

A separate table keeps each row type's constraints strict and its API honest. The two tables share the conventions
of ADR-0025: a stable `code`, a `catalogue_version`, notes that say the data is simulated, fixed UUIDs, integer
minor units, and reads through `modules/wealth/` (the insurance catalogue lives in the wealth module; there is no
new module and no new service).

### 2. Seeded by migration `0020`, versioned, the same in every environment
Like `wealth_products`, `insurance_products` is reference data inserted by its migration (`catalogue_version =
2026-11-v1`). A premium or limit change is a **new** reviewed, reversible migration, never an edit in place, so
every environment and every test sees the same rows and there is an audit trail of what was shown when. Nothing
references the table yet (insurance cannot be bought), so the downgrade simply drops it.

Four products, all marked "illustrative" in their names and "indicative, simulated" in their notes:

| Code | Type | Cover range (INR) | Entry age | Max cover age | Policy terms (years) | Indicative annual premium (INR) |
|---|---|---|---|---|---|---|
| `TERM-LIFE-CLASSIC` | Term life | 25 lakh to 5 crore | 18 to 60 | 75 | 10, 15, 20, 25, 30, 35, 40 | 12,500 for 1 crore cover, age 30, 30-year term |
| `TERM-LIFE-SMALL` | Term life | 10 lakh to 50 lakh | 18 to 65 | 75 | 5, 10, 15, 20 | 7,200 for 25 lakh cover, age 40, 20-year term |
| `HEALTH-INDIVIDUAL` | Health | 3 lakh to 1 crore | 18 to 65 | none (renewable for life) | 1 | 7,800 for 5 lakh cover, age 30 |
| `HEALTH-SENIOR` | Health | 3 lakh to 25 lakh | 60 to 75 | none (renewable for life) | 1 | 32,000 for 5 lakh cover, age 65 |

The premiums are PBIP's own figures, of the order of Indian retail term and health premiums in 2026 for a
non-smoker, but not copied from any insurer.

### 3. One indicative premium at a stated reference profile; no underwriting, no quote engine
An insurance premium depends on age, health, smoking, occupation, cover and term, and only an insurer's
underwriting can set it. PBIP is a simulated bank with no insurer partner, so it does **not** compute a premium for
the customer. Each product carries one premium for a stated reference (cover, age, term), and the API returns the
reference with the figure so the number can never be read as "your premium". We do not scale it by cover or age:
a linear scaling would be an invented pricing model.

Eligibility hints are derived from the stored entry ages only: the ADR-0015 age bands (`18-25` ... `60+`) that
overlap the entry-age range, each marked whether the whole band or only part of it can apply (for example
`TERM-LIFE-CLASSIC` accepts age 60 but not 61, so `60+` is partial). The cover limits are the product's cover range.
The endpoint does **not** read the customer's date of birth, age band, risk profile or any other customer data:
the response is the same for every customer, so it needs no consent (ADR-0015) and leaks nothing. The band
boundaries come from `customers/demographics.py` (one new pure helper, `band_for_age`), so the two can never drift.

### 4. API
`GET /api/v1/wealth/insurance` (optional `insurance_type` filter: `TERM_LIFE`, `HEALTH`), customer-only like the
rest of `/wealth` (`get_current_customer`; staff get 403). Read-only, no idempotency key, no ledger, no audit
record (it changes nothing). Response per product: code, type, name, description, `cover_min_minor`,
`cover_max_minor`, `policy_terms_years`, `indicative_premium` (annual premium in paise + the reference cover, age
and term), `eligibility` (entry ages, maximum cover age, age-band hints), notes and `catalogue_version`.

### 5. Comparison (TASK-098) is computed in the browser from the two catalogues
The comparison page (`/wealth/compare`) reads `GET /api/v1/wealth/products` and `GET /api/v1/wealth/insurance`
and shows the selected 2 to 4 products side by side. A field that does not apply to a product type is shown as
"Not applicable" (for example yield for a health plan, or cover for a deposit), and a market-linked fund's yield
stays "Market-linked: no stated yield", never a number. No new endpoint is needed.

## Alternatives considered
* **Insurance rows in `wealth_products` with a type.** Rejected (section 1): many nullable columns, weakened
  constraints, and a false risk level.
* **Catalogue in code** (as the loan catalogue, ADR-0024). Fails the acceptance criterion "seeded through a
  migration" and gives no versioned audit trail of what was shown.
* **A premium table per age band and cover slab.** More realistic-looking, but it is still invented pricing and
  invites reading it as a quote; one reference premium is honest about what PBIP knows.
* **Personalise eligibility from the customer's age band.** Needs the ADR-0015 consent check and would turn a hint
  into something resembling underwriting; out of scope.

## Consequences
* Good: strict constraints per product kind; the response is identical for all customers; no new module, job,
  dependency or money movement.
* The comparison shows insurance and investments side by side with "Not applicable" for the fields one kind lacks,
  rather than forcing a common shape on the API.
* Buying a policy, nominee handling, a real premium quote and personalised eligibility are not supported and need
  their own ADR (an insurer integration).

## Scope of acceptance
For the TASK-096/098 pull request only: the `insurance_products` table and its seed, the read-only endpoint and
the client-side comparison. Accepted by the product owner on 2026-11-03.
