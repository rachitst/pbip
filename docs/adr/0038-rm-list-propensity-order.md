# ADR 0038: Order the RM customer list by the rule-based product score
- Status: Accepted (under owner's standing approval, 2026-11-03)
- Date: 2026-11-03
- Deciders: Architecture Team
- Builds on: [ADR-0022](0022-rm-insights-detail-and-outreach-log.md) (loan filter and its cost),
  [ADR-0032](0032-rm-customer-assignment.md) (an RM sees only assigned customers)

## Context
TASK-056 (#79) asks for `GET /api/v1/insights/customers` to return "a prioritized list of high-propensity
customers". The list shipped in #223 is ordered by churn risk, then balance, because ordering by product score
needs the recommendation rules (`rules-v1`) to run for every customer in scope, not just for the page. When #223
merged, an RM saw every customer, so that cost grew with the whole bank.

Two things have changed since. ADR-0032 limits an RM to their assigned customers, which is a small portfolio. And
ADR-0022 already accepted scoring every candidate in scope when the loan-propensity filter is used.

The only propensity PBIP has is the rule-based relevance score of the recommendation engine. TASK-054's trained
model is out of scope (the owner excluded it), so the score is not a trained model and not a probability.

## Decision
1. **Additive query parameter.** `order_by` takes `CHURN_RISK` (the default, unchanged) or `PROPENSITY`. With
   `PROPENSITY`, rows are ordered by `top_product_score` (best eligible product, `rules-v1`) from high to low.
   Customers with no eligible product come last. Ties are broken by churn risk (high first), then balance (high
   first), then customer id, so the order and the paging are the same on every request.
2. **Same cost model as the loan filter.** `PROPENSITY` scores every customer in the caller's scope after the
   segment and churn filters, then pages. The default `CHURN_RISK` keeps the bounded cost
   (`test_cost_does_not_grow_with_customers_outside_the_page` still holds).
3. **Scope first.** The ADR-0032 policy is applied before scoring, so the order never shows or counts a customer
   outside an RM's portfolio. The BOLA test covers both orders.
4. **Explainable rows, honestly labelled.** Each row gains `top_product_reason`, the first rule-based reason for
   the top product (the flyout already shows all of them). The list response gains `order` and
   `product_score_version` (`rules-v1`). The UI labels the column "Product match (rule-based)" and says the score
   is neither a trained model nor a probability. Churn risk, inactivity and decline stay on every row.
5. **UI default by role.** The RM insights page opens an RM's list in `PROPENSITY` order, since their portfolio
   is small. An administrator, who sees every customer, opens in `CHURN_RISK` order, which avoids scoring the
   whole bank on page load. Both can switch with the "Order" select, which goes back to page 1.

## Consequences
- An additive v1 contract change (one query parameter and three response fields). `make openapi` regenerates the
  web types.
- An administrator who picks `PROPENSITY` pays a cost linear in the customer count, about 14 SQL statements per
  customer (measured for #343). That is acceptable at demo scale (165 customers), and the page shows a loading
  state. If the bank grows, the follow-up from ADR-0022 still applies: store the rule scores nightly next to
  `customer_segments`.
- No schema change, no new dependency, no external service. Customer-facing responses are unchanged.

## Standing-approval conditions
(1) RBAC/MFA/BOLA unchanged; the scope applies to the new order and is tested. (2) No money movement.
(3) No migration. (4) No new service, dependency or outbound data. (5) The score is the existing rule score,
labelled as such; no invented metric. (6) Within TASK-056 criterion 1.
