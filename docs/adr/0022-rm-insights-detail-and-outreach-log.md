# ADR 0022: RM insights detail, loan-propensity filter and the outreach log
- Status: Proposed (needs owner approval before merge: new table)
- Date: 2026-11-01
- Deciders: Architecture Team

## Context
TASK-066 (#89) builds the Relationship Manager dashboard on the TASK-056 API (`GET /api/v1/insights/customers`).
Its acceptance criteria need three things that API did not provide:

1. a leads table filterable by **loan propensity**. The list only carried the score of the top product (any type),
   and filtering one page in the browser would hide matches on other pages;
2. a customer flyout with **recent transaction volume, health score and churn probability**. Only churn
   signals existed;
3. an **action logger** for calls and notes. The first draft of the UI kept these in the browser's
   `localStorage`: they were not shared between RMs, were lost with the browser, and were not audited.

There is also no RM-to-customer assignment anywhere in the data model or the requirements. UC-12 and the
actors table say only that an RM "reviews customer insights and recommendations".

## Decision
1. **List, additive.** Each row gains `loan_product` and `loan_propensity_score`: the best eligible LOAN-type
   product from the recommendation rules (`rules-v1`) and its relevance score. A new query parameter,
   `min_loan_propensity`, filters on the server before paging. A customer with no eligible loan product never
   matches. The recommendations service gains an optional `top_k` (the default stays 3 for customers), so the
   best loan product is found even when it is not in the customer's top 3.
2. **Cost.** Without that filter, the cost per request stays independent of customers outside the page
   (test `test_cost_does_not_grow_with_customers_outside_the_page`). With it, every candidate after the
   segment and churn filters is scored, because a loan score only exists after the rules run. That is linear in
   the number of customers. It is fine at demo scale, and the UI only sends the filter when the RM picks one.
   If the bank grows, the rules can be materialised nightly next to `customer_segments`.
3. **Detail.** `GET /api/v1/insights/customers/{id}` returns the list row plus `customer_since`, the
   financial health score of the primary account (the same account the recommendation rules use), the 30-day
   posted transaction volume with other parties (own-account moves and reversals excluded), and every eligible
   product with its rule-based reasons. It returns no email, phone, address, date of birth or account number.
   An unknown id gives 404.
4. **Outreach log.** A new table, `rm_interactions`: UUID PK, `customer_id` FK (CASCADE), `staff_user_id` FK
   to `users` (RESTRICT: staff are deactivated, never deleted), `channel` enum CALL/MEETING/EMAIL/NOTE,
   `outcome` enum (6 values), `note` text of 1 to 1000 characters, `created_at`. It is indexed on
   `(customer_id, created_at)` and on `staff_user_id`. The API can only append: `POST` and `GET
   /api/v1/insights/customers/{id}/interactions`. There is no edit or delete. Each insert writes
   `RM_INTERACTION_LOGGED` to the audit chain in the same transaction, with the channel and outcome but **not the
   note**, because free text may hold personal data.
5. **Authorisation.** All routes use the existing `require_roles(RELATIONSHIP_MANAGER, ADMIN, mfa=True)`.
   **Scope is every customer**, as in TASK-056, because no assignment exists. A customer cannot read the notes
   about them (403).
6. **Segment source (bug fix).** The list read `customers.segment`, which nothing writes. It now reads the
   offline job's `customer_segments` table (ADR-0012), with the old column as fallback.

## Consequences
- An additive v1 contract change: `make openapi` regenerates the web types, and no existing field changes.
- One new table and migration `0011` (up and down tested by CI).
- RM notes are stored server-side and appear in the audit trail. Nothing is stored in the browser.
- The churn "probability" stays the rule-based score from TASK-056 (55% balance decline + 45% inactivity). The UI
  labels it as a rule-based score, not a calibrated probability.
- Follow-ups: RM portfolio assignment, if the product owner wants it (it needs a requirement first), and
  materialising product scores if the customer count grows.
