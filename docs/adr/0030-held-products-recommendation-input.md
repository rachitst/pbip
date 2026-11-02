# ADR 0030: Held products as a recommendation input
- Status: Proposed (needs owner confirmation: it changes `pbip_ml/recommendations/contract.py`)
- Date: 2026-11-03
- Deciders: Architecture Team

## Context
TASK-111 (FR-55 / UC-12): the rules-v1 recommendation engine (TASK-054) did not know what a
customer already holds, so it offered a savings account to customers who already have one and a
fixed deposit to customers who just booked one. The engine sits behind the in-process ML contract
(ADR-0007): the API builds a `CustomerRecommendationContext` and `pbip_ml` must not import
`pbip_api`, so holdings can only reach the engine through that contract. Holdings live in three
modules: accounts (`accounts.service.list_accounts`), loans (`loans.service.list_loans`, active
loans only) and wealth (`wealth.service.list_fixed_deposits`). PBIP sells no fund units
(ADR-0025) and issues no cards or insurance, so nobody can hold those.

## Decision
1. **Additive contract change.** `CustomerRecommendationContext` gains
   `held_products: tuple[HeldProduct, ...] = ()`. A `HeldProduct` is a `kind`
   (`SAVINGS_ACCOUNT`, `CURRENT_ACCOUNT`, `PERSONAL_LOAN`, `AUTO_LOAN`, `HOME_LOAN`,
   `FIXED_DEPOSIT`) and an `amount_minor >= 0` (deposit principal, loan outstanding principal, 0 for
   accounts, whose balances already feed `total_balance_minor`). No ids or account numbers: the
   rules do not need them (data minimisation). The default keeps every existing caller and test
   valid, and with no holdings the output is exactly what it was.
2. **What counts as held.** Accounts that are not CLOSED; ACTIVE loans; fixed deposits that are
   ACTIVE or MATURED (a matured deposit is held until it is withdrawn). The recommendations service
   reads them only through the owning modules' service functions, with the authenticated
   customer id, in the same read as the other inputs.
3. **Suppression rules (engine, rules-v1).** A catalogue product the customer holds is dropped after
   eligibility and before ranking:
   * Savings account - held as `SAVINGS_ACCOUNT`; a current account alone does not count.
   * Fixed deposit - held as `FIXED_DEPOSIT`.
   * Personal credit line - held as `PERSONAL_LOAN` (both meet the same need: unsecured personal
     borrowing). Car and home loans are secured and do not count.
   * Rewards card and family cover - no held kind exists, so they are never suppressed.
4. **Explained exception: a deposit top-up.** A further fixed deposit is still offered when the
   customer passes the deposit's eligibility rules AND the remaining balance covers at least
   **6 months** of average spending (`DEPOSIT_TOP_UP_BUFFER_MONTHS`, three times the deposit's own
   2-month floor, so a top-up never eats the emergency buffer). Its first reason then says so, e.g.
   "You already hold 1 fixed deposit (INR 1,00,000.00); this is a top-up because your balance still
   covers 17.0 months of spending, above the 6 months we look for before suggesting another
   deposit." The deposit's own reasons follow unchanged. No other product has a top-up rule.
5. **Output unchanged.** `Recommendation` and the API response keep their shape (the OpenAPI file and
   `schema.d.ts` regenerate identically); holding a product never changes its score, only whether
   it is offered and, for a top-up, one extra reason. `model_version` stays `rules-v1`: a context
   without holdings produces identical output, and the version names the rule family, not the
   input set.

## Alternatives considered
- Pass the catalogue product ids the customer holds: rejected, it puts the bank's mapping from real
  products to recommendation products into the API; the engine owns that mapping.
- Read loans/deposits tables from the recommendations module: rejected, crosses module boundaries.
- Lower the score of held products instead of dropping them: rejected, the acceptance criterion is
  that a held product is not offered again without an explained reason.
- Bump to `rules-v2`: rejected for now (see 5); revisit when the rule family itself changes.

## Consequences
- The customer carousel and the RM insights view (TASK-066, which asks for every eligible product)
  no longer show a held savings account, a held deposit below the top-up line, or the credit line
  to a customer with an active personal loan, so the RM's "best loan product" for such a customer
  is the rewards card, or none.
- Two extra reads per recommendation request (loans and deposits), both indexed by customer.
- When wealth sells fund units, adding a fund kind and mapping it to the index SIP is a further
  additive contract change.
