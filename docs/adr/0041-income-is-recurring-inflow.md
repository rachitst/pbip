# ADR 0041: Income is recurring inflow, not every credit
- Status: Accepted (under owner's standing approval, 2026-11-03)
- Date: 2026-11-03
- Deciders: Architecture Team (issue #381, found in the TASK-089 bug bash)

## Context
Three domains read one income figure from analytics: the financial health score (savings rate and EMI-to-income,
TASK-041), loan eligibility and the assistant's loan answer (`loans/service._income_and_debt`, TASK-046/051, FR-30,
FR-38), and the recommendation engine (`monthly_inflow_minor`, TASK-054). Until now that figure was every POSTED
`DEPOSIT` or `TRANSFER` credit from another party in the 12-month window, divided by the months observed.

A one-off credit therefore counted as salary. A new customer whose only credit was an INR 1,00,000 opening deposit was
told repayments of INR 2,215 were "4% of your income of INR 1,00,000.00" and offered up to INR 22.9 lakh by the
assistant; the savings rate showed 98.8% "Optimal". A regulated lender does not size a loan from one deposit.

The documents already say what was meant: FR-30 and US-17 assess eligibility from "my ledger income", the loan reason
for zero income is "We could not find **regular** income in your account history", and the assistant's FAQ answer
says "your **regular** monthly income". ADR-0015 keeps income ledger-derived (no declared income or employer field).
PBIP has no salary or payroll transaction type: money arrives as `DEPOSIT` (from the clearing account) or `TRANSFER`.

## Decision
A credit is income only when its **source recurs**: the same paying account with the same payment purpose credited
the account in at least **2 distinct calendar months among the running month and the 2 before it**. The purpose is
the payment reference lower-cased with month names, digits and punctuation removed, so "Salary Oct 2026" and
"SALARY - November 2026" from one payer are one source. Every credit of a recurring source inside the 12-month window
counts; nothing from a source that has not recurred recently counts. The rest is unchanged: own-account transfers,
reversals and fixed-deposit movements are still excluded, `FD_INTEREST` is still not income, and the monthly figure
is still the window total divided by the months observed.

The rule lives in one place (`analytics/repository.recurring_income_minor`, constants `INCOME_RECURRENCE_MONTHS = 3`
and `INCOME_MIN_RECURRING_MONTHS = 2`), so the health score, loans, the assistant and recommendations stay
consistent. No schema, API shape or contract changes.

## Alternatives considered
- **Exclude `DEPOSIT` credits.** Simple, but in PBIP every external inflow, including a salary, arrives as a
  `DEPOSIT` from the clearing account, so nobody would have income; and a one-off `TRANSFER` from a friend would still
  count.
- **Exclude credits whose reference says "opening balance".** Keyword matching on free text: misses the next one-off
  ("Gift", "Bonus", "Refund") and is trivially wrong for any reference we did not foresee.
- **Same counterparty only (ignore the reference).** Every `DEPOSIT` has the same payer (clearing), so an opening
  deposit plus any later top-up would look like salary.
- **A declared-income field.** ADR-0015 rejects collecting income or employer data; it would also not be verified.
- **Say "too little history" without changing income.** Hides the symptom on the loan page only; the savings rate
  and recommendations would still be computed from the deposit.

## Consequences
- **Customer-visible:** a customer whose only credit is an opening deposit now has no income. Loan eligibility says
  "We could not find regular income in your account history" and offers no amount; the assistant asks for an amount
  instead of suggesting one; the savings-rate and EMI-to-income components show "no income observed"
  (INSUFFICIENT_DATA); products with a minimum income (credit line, rewards card, index SIP, family cover) are no
  longer recommended to them. Customers with a monthly salary see an income of their salary, not of their balance.
- A salary that stops is no longer income after it misses two of the last three months; a new salary is income from
  its second month. Both are the conservative side for lending.
- The demo seed (`scripts/seed_dev.py`) funds customers with one opening deposit, so seeded customers have no income
  until they receive recurring credits. Back-dating seed history would mean rewriting ledger timestamps, which only
  tests do; a seeded salary history is follow-up work.
- Tests: `make_customer(salary=...)` credits a salary last month and this month (monthly income = `salary`), and
  `pbip_test_support.post_deposit_at` dates a deposit for history fixtures. Reference normalisation is a heuristic: a
  payer that changes its reference wording every month will not be recognised; monitor through support feedback.

Standing approval holds: (1) no security, auth, RBAC, BOLA or audit change; (2) no money movement, only how analytics
reads posted entries; (3) no migration; (4) no new service, dependency or outbound data; (5) no invented data, the
figure is still the customer's own ledger; (6) within FR-30/FR-34/FR-38 and the TASK-041 health score.
