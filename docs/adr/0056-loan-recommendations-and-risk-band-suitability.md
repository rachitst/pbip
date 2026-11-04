# ADR 0056: Catalogue loan recommendations with balance and spending checks, and the risk band as a suitability rule
- Status: Accepted (standing approval, 2026-11-05)
- Date: 2026-11-04
- Deciders: Architecture Team (UC-06, UC-07; FR-30, FR-31, FR-34, FR-12, FR-53)

## Context
The use-case document asks for two things the recommendation engine (`rules-v1`, TASK-054; the served
`sim-propensity-v2` reorderer, ADR-0049/0053) did not do:

- **UC-06 Loan recommendation.** "System analyses account balances, income patterns and spending patterns, generates
  an eligibility assessment, displays recommendations." Loan eligibility (TASK-046) used income and repayments only
  (debt-to-income at most 50%), and the engine suggested an abstract "Personal credit line" and a rewards card, not
  the catalogue loans (`loans/catalogue.py`: personal, car, home) the customer can actually apply for on `/loans`.
- **UC-07 Investment recommendation.** "Inputs: risk tolerance, savings behaviour, income, available funds." The
  RTQ-1 band (TASK-097) is recorded and filters the assistant's investment suggestions (TASK-105, #335), but the
  `/recommendations` ranking ignored it, so the carousel could show a high-risk index-fund SIP to a customer whose own
  questionnaire said LOW.

Constraints: eligibility stays rules first and explainable; the served model may only re-order what the rules allow
(ADR-0049); the `/loans/check-eligibility` contract may only grow; the recommendation context cache (ADR-0054) must
cover every new input or the input must be read live; the customer never sees an internal score.

## Decision

### 1. UC-07: the RTQ-1 band is a suitability rule, not a ranking weight
- `pbip_ml.recommendations.contract` gains `RiskLevel` (LOW, MODERATE, HIGH: the values of `customers.risk.RiskBand`
  and of the wealth catalogue), `BankingProduct.risk_level` and `CustomerRecommendationContext.risk_band`
  (`None` until the questionnaire is answered). These are additive, defaulted fields.
- The index-fund SIP carries `HIGH`, the level of its catalogue row `MF-INDEX`. No other engine product has a risk
  level: the fixed deposit is a SAVINGS product (LOW in the catalogue, suitable for every band), and loans and cover
  are not investments.
- **Rule** (`engine.risk_band_rule`), applied with the eligibility and held-product rules, before any ordering:
  | Band | A product with a risk level above the band | A product at or below the band |
  |---|---|---|
  | LOW (conservative) | not offered | offered; last reason names the band |
  | MODERATE | not offered (so the HIGH-risk SIP is not offered) | offered; last reason names the band |
  | HIGH (aggressive) | n/a | offered; last reason names the band |
  | none recorded | offered as before; last reason says the band was not considered | same |

  This is exactly the assistant's TASK-105 rule (`assistant/discovery.py`: suggested only when the catalogue risk is
  within the band), so the carousel and the assistant never disagree about the same customer.
- **Exclusion, not down-ranking.** A down-ranked unsuitable product would still appear whenever fewer than three
  others are eligible, and the served model could lift it back up. Excluding it is the only form the model cannot
  undo. An aggressive band surfaces the product only when the eligibility rules (3 months' buffer, INR 1,00,000
  balance, 15% savings ratio, health score 55) also allow it; the band never adds a product.
- Reason texts (customer-facing; the band is the customer's own questionnaire result, already shown on Profile &
  security, so nothing internal is revealed):
  - with a band: "Your risk profile from the questionnaire is high; this is a high-risk product, so it fits that
    profile."
  - without: "You have not answered the risk questionnaire under Profile & security, so this suggestion does not take
    your risk tolerance into account yet."
  The band reason comes **after** the eligibility figures, so the first reason is still the customer's own balance.
- The assistant (TASK-105) asks the engine with `apply_risk_band=False`: it applies the same rule itself and must
  still be able to say which product it left out as riskier than the band. Every other caller (the customer route,
  the RM insights view) gets the rule.
- **Model.** The band is not a model feature and the feature list is unchanged, so the promoted `sim-propensity-v2`
  artefact still loads, and it re-orders only the products the band left in. The TASK-054 gate tests
  (`ml/tests/test_recommendation_propensity.py`) are unchanged and pass.

### 2. UC-06: catalogue loans, assessed by the loans module, in the recommendation mix
Implemented in the PR stacked on section 1's (one concern per PR); recorded here as one decision.

**Eligibility (loans module, `loan-eligibility-v2`).** The TASK-046 rules stay, in order, and two rules are added:

| # | Rule | Threshold | Figures |
|---|---|---|---|
| 1 | Recent lockout refuses the check | any lockout in 90 days | security events |
| 2 | Regular income must exist | > 0 | recurring credits (ADR-0043) |
| 3 | Debt-to-income with the new EMI | at most **50%** | ledger repayments + active PBIP EMIs |
| 4 | **Spending with the new EMI** (new) | average monthly spending + active PBIP EMIs + new EMI at most **90%** of income | analytics cash flow |
| 5 | **Balance buffer** (new) | available balance across the customer's accounts at least **1 x** the new EMI | accounts |

- Rule 4 is a residual-income check: whatever the debt ratio, a customer whose spending already takes most of the
  income cannot carry a new EMI. Spending already contains repayments made from the account, and active PBIP EMIs are
  added on top, the same safe-side double count the debt rule documents (ADR-0024). 90% keeps a 10% margin for the
  month-to-month variation of an average.
- Rule 5 makes sure the first instalment can be paid from money already held even if the next salary is late.
- The largest affordable amount per tenor (`tenor_options`, `max_eligible_amount_minor`, and the assistant's FR-38
  suggestion) uses the tightest of the three budgets (50% of income less existing EMIs; 90% of income less spending
  and PBIP EMIs; the balance), so a suggested maximum always passes rules 3-5.
- Rules 4 and 5 compare integers (`spend + PBIP EMIs + EMI <= income * 90 // 100`, `balance >= EMI`); the ratio
  shown is rounded to one decimal place in the reasons.
- `EligibilityOut` gains `monthly_spend_minor`, `available_balance_minor`, `spending_with_loan_ratio` and
  `rules_version` (additive, defaulted). Applying (`POST /loans/apply`) re-runs the same check, so the new rules also gate
  disbursal; the lock order and transaction shape do not change.

**Recommendation.** The engine gets the loans module's own assessment of each catalogue loan as an input
(`CustomerRecommendationContext.loan_offers`, a `LoanOffer` per product: eligible or not, a suggested range from the
product minimum to the largest affordable amount at a reference tenor, the EMI at that maximum, and the loans
module's reasons). The engine never re-decides loan eligibility.
- When loan offers are supplied (always, from the API), they **replace the abstract credit line**: each eligible
  catalogue loan whose type the customer does not already hold (ADR-0030) is offered; the credit line is not.
- Ordering: a catalogue loan takes the score the credit line would have had (rules-v1 relevance, or the served model's
  simulated probability for the credit line, which is the model's borrowing propensity). The model artefact therefore
  needs no new coefficients and no retraining. Only the first eligible loan (catalogue order: personal, car, home)
  competes for the top slots; the others follow every non-loan product, so the carousel shows at most one loan
  unless nothing else is eligible.
- **Offer sizing.** Each catalogue loan is sized at a reference tenor (personal 36, car 60, home 240 months: the
  middle of each product's offered range); if nothing fits there, the next longer offered tenor that fits. The
  suggested range is the product minimum to the largest amount rules 3-5 allow at that tenor; the maximum is itself
  checked through the same `assess` function, so it always passes `/loans/check-eligibility`.
- **Exposure: both.** `GET /api/v1/recommendations` (the carousel) shows the loan in the mix with an additive `loan`
  object (eligibility, suggested range, tenor, EMI at the maximum, rate). A new customer-only, read-only
  `GET /api/v1/loans/offers` returns every catalogue loan with its assessment, eligible or not, with the reasons, for
  the `/loans` page. The loan-specific endpoint is needed because the carousel shows at most three products and never
  explains a refusal; the `/loans` page must show both. The route uses `get_current_customer` (the customer and user
  come from the token, nothing from the request), is in the BOLA matrix and the authentication list, and is covered
  by the route-policy registry like every `get_current_principal` route.
- The RM insights view asks for every product (`recommendations.service.EVERY_PRODUCT`, the engine catalogue plus
  the catalogue loans) instead of the engine catalogue's size, so a trailing catalogue loan is not cut off.
- Each item keeps a stable `product_id` (the loan catalogue id for loans) and `product_type` (LOAN, SAVINGS,
  INVESTMENT, INSURANCE), which the web carousel uses to wire actions.

### 3. Context cache (ADR-0054)
- The risk band is **read live** on every request, through the new `customers.service.recommendation_bands`, which
  returns the ADR-0015 age band and the RTQ-1 band from one read of the customer row (two separate `session.get`
  calls each load the row off the HTTP path, where nothing else holds it). On the customer route that row is already
  in the session, so the band costs no statement; a new questionnaire answer shows on the very next response while
  the cached figures are reused (tested by statement count: the service and HTTP budgets of ADR-0054 are unchanged).
- For UC-06 the figures the loan assessment needs (income, spending, existing repayment ratio, active PBIP EMIs) are
  the ones already cached under the account version: a new or repaid PBIP loan posts to one of the customer's
  accounts and changes the version. Balances are read live (they already were). The lockout count changes without
  touching an account, so it is **read live**: one statement more per request, which the statement-count tests pin.

## Consequences
- The carousel and the assistant apply one suitability rule. A LOW or MODERATE customer no longer sees the index-fund
  SIP on the carousel; a customer without a band sees it with a reason asking them to answer the questionnaire.
- Customers whose spending or balance cannot carry a new EMI are now refused by `/loans/check-eligibility` and
  `/loans/apply`, with a reason quoting their own figures. This is a lending-policy tightening.
- The RM insights view (rules-v1, ADR-0041) sees the same suitability-filtered list.
- The simulated-uptake model is unchanged; its credit-line coefficients now order the catalogue loans. If a future
  model learns per-loan coefficients, that is a retraining under ADR-0053's gate.

## Alternatives considered
- **Band as a ranking weight** (down-rank instead of exclude): rejected; the served model could lift an unsuitable
  product back, and the assistant already excludes.
- **Treat MODERATE as allowed to see HIGH products with a warning**: rejected for consistency with TASK-105; a
  carousel is not the place for a suitability warning.
- **Add the three catalogue loans to the engine catalogue**: rejected; the promoted artefact must cover exactly the
  catalogue, so it would stop loading (fail-safe to rules-v1) until retrained, and loan eligibility would be decided
  twice (engine thresholds and loans module) and could disagree.
- **Recompute loan eligibility from scratch on every recommendation request**: rejected; it repeats the entries pass
  the ADR-0054 cache exists to avoid.

## Validation
- `ml/tests/test_recommendations_risk_band.py` (every band, no band, eligibility first, the model cannot restore an
  excluded product), `apps/api/tests/integration/test_recommendations_risk_band.py` (through the API, live band with
  the cached part reused, the assistant's pre-band list); the TASK-054 gate tests unchanged.
- `apps/api/tests/unit/test_loan_eligibility_rules.py` (rules 4 and 5 at and over their thresholds, every failing
  rule reported, the suggested maximum always eligible, the binding budget, offers), `ml/tests/
  test_recommendations_loan_offers.py` (catalogue loans replace the credit line, the engine never re-decides, held
  types, one loan in the top places, the model orders loans by the credit line's probability and adds none),
  `apps/api/tests/integration/test_loan_offers.py` (rule 4 refuses the check and the application, the offers
  endpoint, the recommendations carry the loans module's assessment), the BOLA matrix, the statement-count tests
  (one more live statement), `Loans.test.tsx` and `ProductRecommendations.test.tsx`.
