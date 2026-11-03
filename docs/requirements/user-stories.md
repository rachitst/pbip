# PBIP user stories

| | |
|---|---|
| Task | TASK-104 (#187) |
| Derived from | *PBIP Use Case Document v0.2*, use cases UC-01 to UC-13 and the section 5 scope |
| Status checked against | `main` at `2e65b87` (1 Nov 2026); the stories changed in batch docs 5 (US-01, US-06, US-08, US-11 to US-16, US-19 to US-21, US-27, US-32 and US-33) against `bae2580` (3 Nov 2026), and in batch docs 6 (US-06, US-18 and US-20) against `fc4279a` (3 Nov 2026) |
| Related | [Requirements specification](requirements-specification.md) (FR/NFR ids), [traceability](traceability.md) (per-use-case code, tests and gaps) |

Each story has:

* acceptance criteria, written as observable behaviour;
* a status: **Implemented** (on `main`, proven by the tests listed), **Partial** (the criteria marked ✗ are not
  met), or **Planned**;
* the task that owns any unmet criteria;
* the tests that prove the met criteria.

Test paths are relative to `apps/api/tests/` unless they start with `apps/web` or `ml/`. A criterion marked ✓ is
proven by a listed test. A criterion marked ✗ is not met on `main`.

The traceability row for each use case is in the
[functional use cases table](traceability.md#functional-use-cases-section-7).

---

## UC-01 Customer login

### US-01 Sign in securely, with MFA when enabled (FR-01, FR-02, FR-03)

*As a retail customer, I want to sign in with my e-mail, password and authenticator code, so that only I can reach my
money.*

**Acceptance criteria:**

* ✓ Valid credentials return an access token and set the refresh cookie. If MFA is enabled, a challenge is returned
  first and access is granted only after a valid TOTP or recovery code.
* ✓ An unknown e-mail and a wrong password get the same response. Each failure is recorded as a `LOGIN_FAILURE`
  security event.
* ✓ After 5 failures the account locks, and the lock time doubles with each further failure. Repeated bad MFA codes
  also lock it.
* ✓ A TOTP code cannot be replayed, and a recovery code works once.
* ✓ The web sign-in form asks for the authenticator code when the server requires MFA.
* ✓ A customer can enrol in MFA from the web app: the secret and an `otpauth` link, a confirmation code, then the
  recovery codes shown once (#300).

**Status:** Implemented.

**Tests:**

* `integration/test_auth.py`: `test_register_login_and_me`, `test_unknown_user_and_wrong_password_are_indistinguishable`,
  `test_progressive_lockout_and_recovery`, `test_mfa_enrolment_login_replay_protection_and_recovery_codes`,
  `test_repeated_bad_mfa_codes_lock_the_account`, `test_login_rate_limit_per_ip`;
* `apps/web/src/pages/Login.test.tsx`: "asks for the authenticator code when the server requires MFA";
* `apps/web/src/pages/MfaEnrolment.test.tsx`: "enrols, confirms a code and shows the recovery codes once",
  "shows the server's message for a wrong code and keeps the form"; `apps/web/src/pages/Register.test.tsx`.

**Tasks:** TASK-004, TASK-006 (Done); registration and MFA enrolment screens (#201, Done #300).

### US-02 Keep my session safe (FR-04)

*As a retail customer, I want stolen or old sessions to stop working, so that a leaked token cannot be used against me.*

**Acceptance criteria:**

* ✓ Refresh tokens rotate on every use. Reusing an old token revokes the whole session family.
* ✓ Logout revokes the session.
* ✓ A password change revokes every session and is audited.
* ✓ Refresh needs the CSRF header and a trusted origin.
* ✓ Forged or invalid access tokens are rejected.

**Status:** Implemented.

**Tests** (`integration/test_auth.py`): `test_refresh_rotation_and_reuse_detection`, `test_logout_revokes_the_session`,
`test_password_change_revokes_all_sessions_across_families`, `test_refresh_requires_csrf_header_and_trusted_origin`,
`test_forged_and_invalid_access_tokens_are_rejected`; E2E `apps/web/e2e/sprint1.spec.ts`.

**Tasks:** TASK-006 (Done).

### US-03 Unlock a locked-out user (FR-05)

*As a banking administrator, I want to see why a user is locked out and clear it, so that legitimate customers regain
access.*

**Acceptance criteria:**

* ✓ ADMIN with MFA can inspect the lockout state and unlock the user. The action is audited.
* ✓ Other roles get 403.
* ✓ The administrator can do this from a screen: `/staff/admin` looks up a user's lockout and clears it after a
  confirmation (TASK-108, #346).

**Status:** Implemented (#346).

**Tests:** `integration/test_admin_lockout.py`; `integration/test_rbac_bola.py::test_admin_can_create_staff_and_it_is_audited`,
`test_only_admins_with_mfa_may_use_admin_routes`; web `apps/web/src/pages/staff/Administration.test.tsx`
("inspects a lockout and clears it after confirmation"), `routing.test.tsx`.

**Tasks:** TASK-007 (Done); TASK-108 (Done: #346, E2E journey #359).

## UC-02 View banking dashboard

### US-04 See my accounts and balances (FR-07, FR-08, FR-13)

*As a retail customer, I want a consolidated view of my accounts, so that I know where my money is.*

**Acceptance criteria:**

* ✓ Each account shows its available, ledger and held balance.
* ✓ I can open a new account (up to five).
* ✓ A total balance across all accounts is shown: total available, ledger and held at the top of the page, computed
  by the server from my own accounts (`GET /api/v1/accounts/summary`, TASK-109).
  * One total per currency; different currencies are never added together.
  * Savings and current accounts only. Loans are shown on `/loans`; fixed deposits are not an account type yet.
  * Joint accounts count in full for each holder. Closed accounts are not counted. Frozen accounts count in the
    ledger and held totals but not in "available", because they cannot pay out.
  * "Available" is ledger minus the amounts on hold, as on each account; an arranged overdraft is not added.

**Status:** Implemented.

**Tests:**

* `apps/web/src/pages/Accounts.test.tsx`: "restores the session, shows balances and the amount on hold",
  "puts the server-computed total balance above the account list (TASK-109)";
* `integration/test_account_summary.py` (exact totals, another customer's account excluded, frozen, closed, joint,
  held amount), `unit/test_account_summary.py` (per currency, system accounts, overdraft),
  `apps/web/src/pages/TotalBalance.test.tsx` (loading, error and retry, empty, per currency);
* `integration/test_accounts.py`: `test_open_savings_account_success`, `test_open_account_enforces_limit_of_five`;
* `apps/web/src/pages/OpenAccount.test.tsx`.

**Tasks:** TASK-005, TASK-019 (Done), TASK-109 (Done #330).

### US-05 See my recent transactions (FR-09, FR-28)

*As a retail customer, I want my latest transactions per account, so that I can spot anything I do not recognise.*

**Acceptance criteria:**

* ✓ Entries are paged with a cursor, newest first.
* ✓ Paging never duplicates or drops rows while new postings arrive.
* ✓ Another customer's account returns 404 before any cursor is used.

**Status:** Implemented.

**Tests** (`integration/test_transaction_pagination.py`): `test_walking_all_pages_yields_every_entry_once_in_order`,
`test_writes_between_pages_never_duplicate_or_drop_rows`, `test_ownership_is_checked_before_any_cursor_is_used`.

**Tasks:** TASK-008, TASK-019 (Done).

### US-06 Understand my spending and financial health (FR-10, FR-11)

*As a retail customer, I want to see where my money goes and how healthy my finances are, so that I can change my
habits.*

**Acceptance criteria:**

* ✓ The page shows spending by category (chart and accessible table), month-over-month trends and the daily burn rate.
* ✓ It shows a 0-100 health score with savings, buffer and debt components and tips.
* ✓ Empty, loading and error states are handled.
* ✓ The score moves with ledger activity and uses closed months only.
* ✓ A balance-forecast chart with the confidence band is shown: the last 30 days of actual balance continue
  into the 30-day projection, the 80%/95% band is labelled "Predicted, not exact", scheduled payments
  (standing orders, loan EMIs) are marked on the line, with tooltips, a text summary and a table (TASK-050).
* ✓ The projection includes the known scheduled payments up to the month end, and the customer is warned once a
  month (bell notification) when the month may end within INR 1,000 of zero or of the overdraft limit
  (TASK-043).
* ✓ An account too new to forecast shows an empty state, not an error: the API returns
  `INSUFFICIENT_HISTORY` (#406, ADR-0044).

**Status:** Implemented.

**Tests:**

* `apps/web/src/pages/Insights.test.tsx` (tests 3 to 11);
* `integration/test_analytics.py`: `test_spending_trends_and_mom_calculations`,
  `test_financial_health_score_moves_with_ledger_activity`,
  `test_health_score_uses_closed_months_only_so_the_calendar_day_does_not_move_it`;
* `integration/test_forecast.py::test_forecast_is_labelled_with_intervals_from_the_account_history`;
* `integration/test_forecast_projection.py` (obligations, threshold, once-a-month alert) and
  `unit/test_balance_projection.py`;
* `apps/web/src/pages/BalanceForecastSection.test.tsx` (including the `INSUFFICIENT_HISTORY` empty state, #406).

**Tasks:** TASK-040, TASK-041, TASK-042, TASK-049 (Done); TASK-043, TASK-050 (Done #318).

### US-07 See products recommended for me (FR-12)

*As a retail customer, I want relevant product suggestions on my dashboard, so that I discover products that fit me.*

**Acceptance criteria:**

* ✓ The API returns at most three products, each with reasons built from my own figures.
* ✓ The results are scoped to the authenticated customer.
* ✓ The dashboard shows the suggestions with "why this was recommended".

**Status:** Implemented (#328): the carousel on `/accounts` shows each product with its reasons behind a keyboard
disclosure.

**Tests:** `integration/test_recommendations.py::test_recommendations_are_scoped_to_the_authenticated_customer`,
`test_recommendations_use_the_customers_real_figures_and_ignore_date_of_birth_without_consent`;
`apps/web/src/pages/ProductRecommendations.test.tsx`.

**Tasks:** TASK-054 (In Progress); TASK-062 (Done #328, #339: the fixed-deposit card opens the booking dialog).

## UC-03 Download account statement

### US-08 Download a PDF statement (FR-15)

*As a retail customer, I want to download a statement for an account and period as a PDF, so that I have an official
record.*

**Acceptance criteria:**

* ✓ I choose an account and a period, then click Download (PDF or CSV, `/statements`).
* ✓ The PDF has a bank header and the opening and closing balances.
* ✓ Only the account holder can generate it; another customer's account is 404 and nothing is recorded.

**Status:** Implemented (#302).

**Tests:**

* `integration/test_statements.py`: `test_pdf_has_the_bank_header_summary_and_balances`,
  `test_opening_balance_carries_history_before_the_period`, `test_another_customer_gets_404_and_nothing_is_recorded`,
  `test_a_statement_needs_authentication`, `test_invalid_periods_and_formats_are_rejected`;
* `integration/test_rbac_bola.py::test_bola_customer_b_cannot_touch_customer_a_objects` (statement cases);
* `unit/test_statements_render.py`;
* `apps/web/src/pages/Statements.test.tsx`: "downloads the chosen format through an authenticated blob request",
  "checks the period before calling the API and shows server errors".

**Tasks:** TASK-016, TASK-074 (Done #302).

## UC-04 Transfer money

### US-09 Send money safely (FR-18 to FR-22)

*As a retail customer, I want to send money to a payee and confirm it, so that payments are deliberate and protected.*

**Acceptance criteria:**

* ✓ A transfer posts balanced ledger entries. Insufficient funds change nothing.
* ✓ Only my own verified payees can receive money.
* ✓ The per-transfer limit (₹2,00,000) and the daily limit (₹5,00,000, shared with bills) are enforced.
* ✓ Every transfer is fraud-scored:
  * a BLOCK declines without moving money;
  * a REVIEW holds the funds;
  * the customer response hides the reasons.
* ✓ A retry with the same Idempotency-Key returns the original result. Concurrent duplicates create exactly one
  transfer. The same key with a different request is rejected.
* ✓ The web form sends an exact minor-unit amount with an Idempotency-Key and refuses invalid amounts.
* ✓ I choose the source account, and the form refuses more than that account has available.
* ✓ A review step appears before I confirm, then the real outcome is shown (posted, under review, or declined
  without any reason or score).

**Status:** Implemented. Browser journeys: `apps/web/e2e/journey-transfer.spec.ts` and `journey-fraud-review.spec.ts` (TASK-077).

**Tests:**

* `integration/test_transfers.py`: `test_transfer_moves_money_and_writes_balanced_entries`,
  `test_unverified_payee_cannot_receive_money`, `test_per_transfer_and_daily_limits`,
  `test_block_declines_without_moving_money_and_hides_the_reasons`,
  `test_retry_with_same_key_returns_the_original_result_without_a_duplicate`,
  `test_concurrent_duplicates_create_exactly_one_transfer`, `test_same_key_with_a_different_request_is_rejected`;
* `integration/test_ledger.py`: `test_insufficient_funds_changes_nothing`, `test_concurrent_spending_never_overdraws`;
* `apps/web/src/pages/Accounts.test.tsx`: "sends an exact minor-unit amount with an Idempotency-Key header",
  "refuses an invalid amount without calling the API";
* `apps/web/src/pages/SendMoney.test.tsx`: "sends from the chosen account after review and shows the receipt",
  "refuses more than the chosen account has available, without calling the API",
  "shows a declined transfer without any reason or score".

**Tasks:** TASK-033, TASK-037 (Done).

### US-10 Confirm a flagged transfer with my authenticator (FR-21)

*As a retail customer, I want to confirm a transfer the bank considers risky with a code, so that genuine payments
still go through.*

**Acceptance criteria:**

* ✓ A STEP_UP transfer completes after a valid TOTP code.
* ✓ Without MFA enrolled, it escalates to analyst review instead.
* ✓ The web app shows a code prompt. A wrong code can be retried; after 3 wrong codes for the same transfer the
  server holds the funds and hands it to an analyst, and the prompt says it is under review.
* ✓ Closing the prompt leaves the transfer waiting for a code.

**Status:** Implemented.

**Tests:**

* `integration/test_transfers.py`: `test_step_up_with_mfa_completes_after_a_valid_code`,
  `test_step_up_without_mfa_escalates_to_analyst_review`,
  `test_three_wrong_step_up_codes_hand_the_transfer_to_an_analyst`,
  `test_a_correct_code_after_two_wrong_ones_still_completes`;
* `apps/web/src/pages/SendMoney.test.tsx`: "asks for the TOTP on STEP_UP; a wrong code can be retried, a right one completes", "closes the modal and says the transfer is under review when the server escalates", "leaves a step-up transfer waiting if the customer closes the prompt".

**Tasks:** TASK-037 (Done).

## UC-05 Manage payees

### US-11 Add and verify a payee (FR-23)

*As a retail customer, I want to add a payee and check the name matches the account, so that I do not pay the wrong
person.*

**Acceptance criteria:**

* ✓ A payee can be added.
* ✓ A name mismatch leaves it unverified and never reveals the real name.
* ✓ Another customer's payee is 404.
* ✓ I can add and verify payees in the web app (`/payees`).

**Status:** Implemented (#301).

**Tests:** `integration/test_transfers.py::test_payee_name_mismatch_is_not_verified_and_never_reveals_the_real_name`;
`integration/test_rbac_bola.py::test_bola_customer_b_cannot_touch_customer_a_objects`;
`apps/web/src/pages/Payees.test.tsx`: "adds a payee, runs the name check and warns on a mismatch without revealing the
name", "retries only the name check when the payee was already created".

**Tasks:** TASK-020 (Done #301).

### US-12 Edit a payee (FR-24)

*As a retail customer, I want to correct a payee's nickname or name, so that my list stays accurate.*

**Acceptance criteria:**

* ✓ `PATCH /payees/{id}` updates an owned payee. A non-owned payee is 404.
* ✓ A name change forces re-verification.
* ✓ The account number is immutable.
* ✓ Each edit is audited, with the changed field names only; an edit that changes nothing is not.
* ✓ I can edit a payee in the web app, which warns that a new name must be verified again.

**Status:** Implemented (#301).

**Tests:**

* `integration/test_payee_edit.py`: `test_nickname_change_keeps_the_payee_verified_and_is_audited`,
  `test_renaming_resets_verification_and_blocks_transfers_until_verified_again`, `test_same_name_is_not_a_change`,
  `test_account_details_are_immutable_and_input_is_validated`, `test_another_customer_cannot_edit_and_gets_404`,
  `test_a_removed_payee_with_history_cannot_be_edited`;
* `integration/test_rbac_bola.py::test_bola_customer_b_cannot_touch_customer_a_objects` (edit-payee cases);
* `apps/web/src/pages/Payees.test.tsx`: "edits the nickname, warns that a new name needs re-verification, and sends
  only changes".

**Tasks:** TASK-094, TASK-020 (Done #301).

### US-13 Remove a payee (FR-25)

*As a retail customer, I want to delete a payee I no longer use.*

**Acceptance criteria:**

* ✓ `DELETE /payees/{id}` removes an owned payee. A non-owned payee is 404.
* ✓ I can delete a payee from the web app, after a confirmation.

**Status:** Implemented (#301).

**Tests:** `integration/test_rbac_bola.py::test_bola_customer_b_cannot_touch_customer_a_objects` (delete-payee case);
`apps/web/src/pages/Payees.test.tsx`: "removes a payee after confirmation".

**Tasks:** TASK-020 (Done #301).

## Section 5 payment scope (no single use case)

### US-14 Pay a bill (FR-26)

*As a retail customer, I want to pay a utility bill from my account, so that I do not need another channel.*

**Acceptance criteria:**

* ✓ Paying a bill posts balanced entries and is idempotent.
* ✓ It counts toward the shared daily limit and is fraud-scored. STEP_UP and REVIEW hold the funds and open an
  alert, and the analyst's decision settles the payment. BLOCK declines it and opens an alert.
* ✓ Insufficient funds leave no partial state.
* ✓ A receipt and a notification are recorded.
* ✓ I can pay a bill from the web app (`/bills`): an exact minor-unit amount with one Idempotency-Key, a receipt, and
  a plain message when the payment is held or declined.

**Status:** Implemented (#301).

**Tests** (`integration/test_bills.py`): `test_pay_bill_posts_balanced_double_entry_and_records_audit_and_notification`,
`test_idempotent_retry_returns_original_receipt_and_increments_metric`,
`test_bill_payments_respect_the_per_payment_and_shared_daily_limits`,
`test_a_flagged_bill_payment_is_held_for_review_like_a_transfer`,
`test_the_analysts_decision_settles_the_held_bill_payment`,
`test_a_blocked_bill_payment_is_declined_and_opens_an_alert`,
`test_pay_bill_insufficient_funds_leaves_zero_partial_state`; `apps/web/src/pages/PayBills.test.tsx`: "pays in integer
minor units with one idempotency key and shows the receipt", "reuses the same idempotency key when the same confirmed
payment is retried", "says a held payment is on hold and a declined one was not paid".

**Tasks:** TASK-014 (Done); TASK-020 (Done #301).

### US-15 Schedule a recurring transfer (FR-27)

*As a retail customer, I want standing orders, so that regular payments happen without me.*

**Acceptance criteria:**

* ✓ I can create, list and cancel schedules, through the API and the `/scheduled` screens (TASK-021, #306).
* ✓ Each occurrence runs exactly once, with key `sched-{id}-{date}`.
* ✓ Insufficient funds notify me without breaking the schedule.

**Status:** Implemented (ADR-0023).

**Tests:** `integration/test_scheduled_transfers_engine.py::test_concurrent_workers_on_one_due_occurrence_post_it_exactly_once`,
`::test_insufficient_funds_retryable_failure_and_same_day_retry`; `integration/test_scheduled_transfers_api.py`;
`apps/web/src/pages/ScheduledTransfers.test.tsx` ("lists upcoming orders with payee, frequency and next date",
"cancels optimistically through DELETE and restores the row if the API refuses").

**Tasks:** TASK-015 (Done #296), TASK-021 (Done #306).

### US-16 Search my transactions (FR-14)

*As a retail customer, I want to filter my transactions by date and amount, so that I can find a payment quickly.*

**Acceptance criteria:**

* ✓ Date and amount filters (and direction and reference text) work with cursor pagination, in the API and on the
  transactions page.
* ✓ The filtered query reads the account's rows through an account index (`ix_transaction_entries_account_id_*`),
  never a sequential scan of the entries table.

**Status:** Implemented (#303).

**Tests:**

* `integration/test_transaction_search.py`: `test_amount_direction_and_text_filters`,
  `test_date_range_is_bank_days_inclusive`, `test_cursor_walks_the_filtered_list_without_gaps_or_repeats`,
  `test_invalid_filters_are_rejected`, `test_sql_in_the_search_is_just_text`,
  `test_another_customer_cannot_search_my_account`, `test_filtered_query_uses_an_account_index`;
* `apps/web/src/pages/AccountTransactions.test.tsx`: "sends the filters in paise and keeps them on the next page's
  cursor (TASK-017)", "checks the filters locally and clears them".

**Tasks:** TASK-017 (Done #303).

### US-17 Manage my profile and devices (FR-16)

*As a retail customer, I want to see my profile, my devices and change my password, so that I control my account
settings.*

**Acceptance criteria:**

* ✓ The profile shows a masked phone number.
* ✓ Devices can be registered and listed, and another customer's device is 404.
* ✓ The password can be changed, which signs out other sessions.

**Status:** Implemented.

**Tests:**

* `integration/test_customers.py`: `test_get_customer_me_with_masked_phone`, `test_list_and_get_devices_bola_isolation`;
* `apps/web/src/pages/Profile.test.tsx`;
* E2E `apps/web/e2e/sprint1.spec.ts`: "customer opens an account, registers a device and rotates their password".

**Tasks:** TASK-004, TASK-006 (Done).

### US-18 Be told what happened to my payment (FR-17)

*As a retail customer, I want notifications when a payment posts, is held or is declined, so that I am never left
guessing.*

**Acceptance criteria:**

* ✓ The outbox relay turns events into notifications.
* ✓ The bell shows the unread count (capped at 99+).
* ✓ I can mark one or all notifications as read.
* ✓ I cannot read or mark another user's notifications.

**Status:** Implemented.

**Tests:**

* `integration/test_notifications.py`: `test_list_notifications_pagination_and_unread_filtering`,
  `test_bola_cannot_read_or_mark_other_users_notifications`;
* `integration/test_notification_links.py`;
* `integration/test_outbox_relay.py` (retries with backoff, dead letters, exactly-once delivery after a worker crash;
  TASK-026, #393);
* `apps/web/src/shell/NotificationBell.test.tsx`.

**Tasks:** TASK-018, TASK-022, TASK-026 (Done #393).

## UC-06 Loan recommendation

### US-19 Check what a loan would cost (FR-29)

*As a retail customer, I want to see the loan catalogue and calculate EMIs, so that I can plan a borrowing decision.*

**Acceptance criteria:**

* ✓ The catalogue lists the products.
* ✓ The EMI calculation returns an exact integer-paise schedule.
* ✓ I can use a loan calculator in the web app (`/loans`: principal and tenure sliders, EMI from the server).

**Status:** Implemented.

**Tests:** `integration/test_loans.py::test_catalogue_and_emi_schedule`; `unit/test_loan_emi.py::test_known_emi_values`;
`apps/web/src/pages/Loans.test.tsx`.

**Tasks:** TASK-045 (Done); TASK-051 (Done #309).

### US-20 Know whether I am eligible, and why (FR-30, FR-31)

*As a retail customer, I want an eligibility assessment based on my own finances and personalised loan suggestions, so
that I only apply for what I can afford.*

**Acceptance criteria:**

* ✓ Eligibility uses my ledger income and existing repayments, with debt-to-income at most 50%. Income counts only
  credits that recur in 2 of the last 3 months, so a one-off deposit is not income (#404, ADR-0043).
* ✓ The reasons show the figures.
* ✓ A recent lockout refuses the check.
* ✓ Asked about a loan without an amount, the assistant suggests the largest catalogue loan I am eligible for, at
  the catalogue rate (#331); `/loans` shows the eligibility pre-check with the maximum amount for the loan I pick.
* ✗ Personalised suggestions shown without asking are not drawn from the loan catalogue: the recommendation engine
  and the carousel suggest a credit line and a rewards card, and the credit-line card opens `/loans` with no
  catalogue loan preselected.

**Status:** Partial. Catalogue loan suggestions in the recommendations are TASK-054.

**Tests** (`integration/test_loans.py`): `test_eligible_with_explained_figures`,
`test_a_one_off_opening_deposit_is_not_income_so_no_loan_is_offered`,
`test_a_salary_paid_in_two_months_is_income_even_with_month_named_references`,
`test_rejected_when_dti_would_exceed_half_of_income`, `test_rejected_after_a_recent_lockout`;
`integration/test_assistant.py::test_a_loan_without_an_amount_suggests_the_largest_eligible_one`;
`apps/web/src/pages/Loans.test.tsx`: "shows the eligibility pre-check with the maximum amount, the rate and the
reasons".

**Tasks:** TASK-046 (Done); TASK-054 (In Progress); TASK-062 (Done #328, #339); TASK-110 (Done #331).

### US-21 Apply for and track a loan (FR-32)

*As a retail customer, I want to apply for a loan and follow its repayments.*

**Acceptance criteria:**

* ✓ An application re-checks eligibility and creates the full repayment schedule in one transaction.
* ✓ Disbursal posts through the ledger (DEBIT clearing, CREDIT my account); a retried submit never disburses twice.
* ✓ Monthly EMIs are collected by the scheduled transfers engine; a missed EMI is retried, not skipped (ADR-0024).
* ✓ The active loan shows its remaining balance, amortisation progress and upcoming EMIs (web `/loans`).

**Status:** Implemented.

**Tests** (`integration/test_loan_applications.py`): `test_apply_disburses_books_the_schedule_and_links_repayments`,
`test_not_eligible_is_refused_with_plain_reasons_and_leaves_no_state`,
`test_concurrent_duplicate_applies_create_exactly_one_loan_and_one_disbursal`,
`test_a_repayment_marks_the_right_instalment_paid_and_reduces_the_balance`,
`test_a_missed_repayment_is_retried_and_arrears_are_collected_together`; `apps/web/src/pages/Loans.test.tsx`.

**Tasks:** TASK-047, TASK-051 (Done #309).

## UC-07 Investment recommendation

### US-22 Record my risk tolerance (FR-33)

*As a retail customer, I want to answer a short questionnaire, so that suggestions match the risk I accept.*

**Acceptance criteria:**

* ✓ The questionnaire produces a band of LOW, MODERATE or HIGH (five enumerated answers worth 0-2 points each;
  0-3 LOW, 4-6 MODERATE, 7-10 HIGH, questionnaire RTQ-1 in `modules/customers/risk.py`).
* ✓ The profile is readable and updatable only by me (`GET`/`PUT /api/v1/customers/me/risk-profile`).
* ✓ The assessment date and answers are kept, and every assessment is audited.
* ✓ Profile & security has a "Risk profile" questionnaire.

**Status:** Implemented. The band is not yet an input to investment suggestions (US-23).

**Tests:** `integration/test_risk_profile.py`; `unit/test_customer_risk_and_age_band.py`;
`apps/web/src/pages/RiskProfileSection.test.tsx`.

**Tasks:** TASK-097 (Done #326).

### US-23 Get suitable investment suggestions (FR-34 to FR-36)

*As a retail customer, I want investment products suggested from my savings, income and available funds, and to
compare them, so that I can invest sensibly.*

**Acceptance criteria:**

* ✓ The recommendation engine suggests a fixed deposit or an index-fund SIP only when income, savings ratio, buffer
  and balance qualify.
* ✓ The assistant's investment suggestions use my risk tolerance (TASK-105, #335). ✗ The `/recommendations`
  ranking does not use it yet.
* ✓ A wealth catalogue lists FDs, funds and a sovereign gold bond with tenure, APY and lock-in, filterable by risk
  level; funds state no yield because PBIP has no market data (TASK-063, ADR-0025).
* ✓ I can book a fixed deposit from my account with an exact maturity preview, see it with a maturity countdown in
  my portfolio, and withdraw it at maturity or close it early at the penalty rate (TASK-064, TASK-065).
* ✓ Insurance products are listed: two term-life and two health plans with cover range, an indicative premium at a
  reference profile and entry-age hints, without underwriting (TASK-096, ADR-0028).
* ✓ Two to four products can be compared side by side, with "Not applicable" where a field does not apply
  (`/wealth/compare`, TASK-098).
* ✓ When my fixed deposit matures, the principal and interest are credited to its source account automatically,
  exactly once, within the hour, and I get a notification; if that account is frozen or closed, I am told the payout
  is on hold, and it is paid when the account is active again (#332, ADR-0029).

**Status:** Partial.

**Tests:** `ml/tests/test_recommendations.py::test_eligibility_exclusion_for_high_debt_ratio`;
`test_wealth.py::test_catalogue_lists_deposits_funds_and_the_gold_bond_with_yield_and_lock_in`,
`test_catalogue_filters_by_risk_level`, `test_booking_posts_one_balanced_entry_and_stores_the_deposit`,
`test_premature_closure_pays_the_penalty_rate_and_balances`,
`test_maturity_is_computed_on_read_and_pays_exactly_the_projected_amount`; `test_wealth_deposit_math.py`;
`Wealth.test.tsx`; `integration/test_insurance.py`, `unit/test_wealth_insurance.py`, `WealthCompare.test.tsx`;
`integration/test_fd_maturity_payout.py` (`test_a_matured_deposit_is_paid_once_to_its_account_audited_and_notified`,
`test_concurrent_payout_runs_pay_each_deposit_exactly_once`, `test_an_inactive_account_holds_the_payout_notifies_once_and_pays_when_active`).

**Tasks:** TASK-054 (In Progress); TASK-063, TASK-064, TASK-065 (Done #327); TASK-097 (Done #326: the band is recorded,
not yet used here); TASK-096, TASK-098 (Done #337); #332 (fixed in #341).

## UC-08 AI assistant interaction

### US-24 Ask about my own money in plain language (FR-37, FR-38)

*As a retail customer, I want to ask questions like "How much did I spend last month?", so that I get answers without
navigating reports.*

**Acceptance criteria:**

* ✓ Balance, recent-transaction, spending (including last month), forecast and loan-affordability questions are
  answered from my own data.
* ✓ Clear questions never call the model.
* ✓ Answers stream into the chat drawer.
* ✓ If the model is down or wrong, deterministic rules answer.
* ✓ "Can I afford a personal loan?" without an amount is answered (FR-38): the assistant offers the largest amount
  the TASK-046 eligibility rules allow for the product's default tenure (personal 36, car 60, home 240 months, or the
  tenure I name), with the EMI, the catalogue rate and the eligibility reasons. When eligibility cannot be computed
  (no income history, or a sign-in lockout in the last 90 days) it says which and asks for an amount; when existing
  repayments leave no room it says so. No amount or tenure the model invented is used.

**Status:** Implemented.

**Tests:**

* `integration/test_assistant.py`: `test_rules_engine_answers_each_tool_from_own_data`,
  `test_fallback_takes_over_when_the_model_is_down_or_wrong`,
  `test_a_loan_amount_the_customer_never_wrote_is_not_answered`,
  `test_a_loan_without_an_amount_suggests_the_largest_eligible_one`,
  `test_a_loan_without_an_amount_asks_for_one_when_eligibility_cannot_be_computed`,
  `test_a_loan_without_an_amount_when_repayments_leave_no_room`;
* `unit/test_assistant_routing.py::test_clear_questions_take_the_fast_path`;
* `apps/web/src/assistant/AssistantDrawer.test.tsx`.

**Tasks:** TASK-057 to TASK-061 (Done); TASK-110 (#284, Done #331: FR-38 without an amount).

### US-25 Get help, product discovery, investment ideas and unusual-expense explanations (FR-39, FR-40)

*As a retail customer, I want the assistant to answer FAQs, show suitable products, suggest investments and point out
unusual expenses.*

**Acceptance criteria:**

* ✓ The read-only tools `get_faq_answer`, `list_products` and `get_investment_suggestions` exist, with bounded enum
  arguments or none, and no identity argument (TASK-105, #335). FAQ answers come from a curated, versioned source
  (FAQ-1) whose figures are read from the code and the effective runtime limits.
* ✓ Investment suggestions use my risk profile and the recommendation engine's output; without an assessed profile
  the assistant says so.
* ✓ Adversarial tests cover the new tools: injection in a catalogue name, identity injected by the model,
  cross-customer probes, SQL and override text.
* ✓ "Show unusual expenses" (FR-39) lists, for this or last month, the categories at least 1.5x my average month of
  the previous three and the single payments at least 3x my usual (median) payment in that category, each at least
  INR 1,000 above usual and each with the figures that make it unusual. Merchant names are quoted data, stripped and
  capped. Rule: `analytics/unusual.py`.

**Status:** Implemented (#331, #335). The FAQ has 8 topics; suggestions are limited to the engine's fixed deposit and
index-fund SIP; `list_products` does not list insurance.

**Tests:** `unit/test_unusual_expenses.py`; `integration/test_assistant.py`:
`test_unusual_expenses_lists_exactly_the_items_outside_the_own_baseline`,
`test_unusual_expenses_when_nothing_stands_out_or_history_is_short`; `integration/test_assistant_adversarial.py`:
`test_injection_in_a_merchant_name_stays_quoted_data_and_selects_nothing`,
`test_cross_customer_unusual_expense_probes_never_return_the_victims_items`,
`test_injection_in_a_product_name_stays_quoted_data_and_selects_nothing`,
`test_a_model_cannot_inject_identity_or_free_text_into_the_new_tools`,
`test_cross_customer_investment_probes_never_return_the_victims_profile`,
`test_sql_and_override_text_around_the_discovery_intents_is_refused`; `integration/test_assistant.py`:
`test_investment_suggestions_need_an_assessed_risk_profile`,
`test_investment_suggestions_follow_the_risk_band_and_the_engine_reasons`,
`test_products_are_listed_from_the_real_catalogues_with_their_exact_rates`,
`test_the_faq_quotes_the_limits_an_admin_set_at_runtime`; `unit/test_assistant_faq.py`,
`unit/test_assistant_discovery.py`, `unit/test_assistant_routing.py::test_registry_is_exactly_the_approved_read_only_tools`.

**Tasks:** TASK-110 (#284, Done #331); TASK-105 (#188, Done #335).

### US-26 Trust that the assistant cannot be abused (FR-41)

*As a retail customer, I want the assistant to be unable to move money or reveal anyone else's data, even when someone
tries to trick it.*

**Acceptance criteria:**

* ✓ The tool registry is exactly the approved read-only tools, and the customer id is injected server-side.
* ✓ Money-movement, other-customer, SQL, instruction-override and privilege requests are refused, including
  base64-encoded ones.
* ✓ The output filter masks account numbers, tokens and e-mails.
* ✓ Every tool call and refusal is audited.

**Status:** Implemented.

**Tests:**

* `integration/test_assistant_adversarial.py::test_cross_customer_probes_never_return_the_victims_data`;
* `unit/test_assistant_routing.py::test_registry_is_exactly_the_approved_read_only_tools`;
* `unit/test_assistant_hardening.py`: `test_output_filter`, `test_base64_encoded_instructions_are_decoded_and_refused`.

**Tasks:** TASK-057, TASK-059, TASK-067, TASK-076 (Done).

## UC-09 Fraud detection

### US-27 Score every payment for fraud with reasons (FR-42 to FR-47)

*As a fraud analyst, I want every transfer and bill payment scored with readable reasons, so that risky payments are
stopped and I can see why.*

**Acceptance criteria:**

* ✓ Every scoring decision is persisted with the score, reasons, feature snapshot, model version and latency.
* ✓ Large amount to a new payee: `NEW_PAYEE_LARGE_AMOUNT`.
* ✓ Multiple login failures: `RECENT_LOGIN_FAILURES`.
* ✓ Rapid consecutive transactions: `VELOCITY_BURST`. It counts in Redis and falls back to PostgreSQL.
* ✓ Unusual amount: `AMOUNT_SPIKE`.
* ✓ New country: `NEW_COUNTRY`. New device or network: `NEW_DEVICE_OR_NETWORK`.
* ✓ With `FRAUD_SCORER=hybrid`, LightGBM, Isolation Forest and SHAP reasons are added. Any model failure falls back
  to the rules, never to allow-all.
* ✓ The local and demo configuration serve the hybrid scorer (#251 fixed: legitimate first-time behaviour in the
  generator, retrained models). The code default and the staging compose stay `rules`.
* ✓ Location is assessed below country level: `IMPOSSIBLE_TRAVEL` compares the payment's city with the customer's
  last located sign-in and fires when the speed needed is impossible (DB-IP City Lite, ADR-0034). Without the city
  file the rule switches off and the payment path is unchanged. Its accuracy is not evaluated (no labelled location
  data).
* ✗ The standalone `LARGE_AMOUNT` rule has a dedicated test: no test asserts the `LARGE_AMOUNT` code on its own;
  only `NEW_PAYEE_LARGE_AMOUNT` is tested.

**Status:** Partial.

**Tests:**

* `integration/test_transfers.py`: `test_every_scoring_decision_is_persisted_for_audit_and_retraining`,
  `test_new_payee_plus_large_amount_is_held_with_human_readable_reasons`, `test_velocity_burst_is_flagged`,
  `test_amount_spike_against_history_is_flagged`, `test_transfer_from_a_new_country_adds_a_new_country_reason`;
* `integration/test_fraud_signals.py`: `test_recent_login_failures_rule`,
  `test_velocity_falls_back_to_postgresql_when_redis_is_down`,
  `test_new_network_fires_and_the_full_ip_is_never_stored`;
* `integration/test_fraud_hybrid.py::test_any_model_problem_falls_back_to_the_rules_never_allow_all`;
* `ml/tests/test_fraud_behaviour_regression.py`;
* `integration/test_fraud_impossible_travel.py`:
  `test_sign_in_in_london_then_payment_from_mumbai_two_hours_later_is_impossible`,
  `test_the_same_two_places_fourteen_hours_apart_are_plausible`,
  `test_a_nearby_city_five_minutes_earlier_is_below_the_distance_floor`,
  `test_transfer_flow_fills_the_location_and_persists_the_travel_snapshot`,
  `test_bill_payment_fills_the_location_and_is_scored_for_travel`,
  `test_with_no_city_database_the_payment_path_is_unchanged`; `unit/test_fraud_travel.py`.

**Tasks:** TASK-023 to TASK-034, TASK-039, TASK-080, TASK-095 (Done); #251 (closed by #257); impossible travel (#365,
post-merge review fixed in #372).

## UC-10 Fraud alert management

### US-28 Customers are told when a payment is held or declined (FR-48)

*As a retail customer, I want to know when my payment is under review or declined, so that I can act.*

**Acceptance criteria:**

* ✓ REVIEW places a hold, opens an alert and notifies `TRANSFER_UNDER_REVIEW`.
* ✓ The decision later notifies posted or declined.

**Status:** Implemented.

**Tests:** `integration/test_transfers.py::test_review_holds_funds_and_analyst_release_posts_the_transfer`;
`integration/test_notification_links.py`.

**Tasks:** TASK-018, TASK-022 (Done).

### US-29 Review and resolve alerts (FR-48, FR-49)

*As a fraud analyst, I want a queue of alerts with the score, reasons and features, and to release or reject each, so
that potential fraud is addressed promptly.*

**Acceptance criteria:**

* ✓ FRAUD_ANALYST or ADMIN with MFA can list alerts and see an alert's details.
* ✓ RELEASE posts the held transfer.
* ✓ REJECT releases the hold and declines.
* ✓ Resolution is audited, and a second resolution is 409.
* ✓ An analyst screen lists open alerts and shows each alert with plain-language reasons, SHAP bars and the
  customer profile, with release and reject actions. Staff without the analyst role never call the API.
* ✓ Fraud statistics (decisions over time, top reasons, masked risky payees) are available to analysts with MFA.

**Status:** Implemented. The statistics are not charted in the UI yet: the security dashboard (US-34, TASK-099) shows flagged events and open alerts, not the `/fraud/stats` trends.

**Tests:**

* `integration/test_transfers.py`: `test_review_holds_funds_and_analyst_release_posts_the_transfer`,
  `test_analyst_reject_releases_the_hold_and_declines`;
* `integration/test_rbac_bola.py::test_staff_endpoints_demand_mfa_in_the_session`;
* `integration/test_fraud_stats.py`: `test_stats_report_decisions_rules_and_masked_payees`,
  `test_stats_are_staff_only_and_need_mfa`;
* `apps/web/src/pages/staff/Fraud.test.tsx`: "lists open alerts and switches filters through the API", "explains the score in plain language with SHAP bars and the customer profile", "does not call the API for staff without the analyst role".

**Tasks:** TASK-035, TASK-036 (Done).

## UC-11 Customer behaviour analytics

### US-30 Capture how customers use the app (FR-50, FR-51)

*As a relationship manager, I want page visits, clicks, session length and product interest captured, so that
behavioural profiles reflect real engagement.*

**Acceptance criteria:**

* ✓ `POST /events/batch` accepts typed events into monthly partitions. The event types are page_view,
  loan_calculator_used, transfer_initiated, account_viewed, product_clicked and search_performed.
* ✓ Free text, URLs and unknown fields are rejected.
* ✓ No IP address or user agent is stored.
* ✓ Ingestion is rate-limited.
* ✓ The web app sends events: page visits as route templates only and five allow-listed interactions, in bounded
  batches whose failures are dropped; nothing is sent when signed out, in the staff area or under Global Privacy
  Control (TASK-112, `docs/analytics/clickstream-events.md`).
* ✗ Session duration is recorded.
* ✗ Product interests are derived.

**Status:** Partial.

**Tests** (`integration/test_events.py`): `test_events_batch_happy_path`,
`test_events_reject_extra_fields_free_text_and_urls`, `test_no_ip_address_or_user_agent_is_stored`,
`test_events_partition_routing_across_months`, `test_a_batch_built_by_the_web_tracker_is_accepted_and_stored_as_sent`;
web `telemetry/tracker.test.ts`, `telemetry/PageViews.test.tsx`.

**Tasks:** TASK-055 (In Progress); TASK-101 (Done #325: reads the events); TASK-112 (Done #333).

### US-31 Build a behavioural profile from spend and income (FR-52)

*As a relationship manager, I want each customer assigned an explainable segment from their spending and income, so
that I understand the customer base.*

**Acceptance criteria:**

* ✓ A weekly job assigns one of five segments from RFM and lifestyle features. It is deterministic and idempotent.
* ✓ The customer's segment is served by the API.

**Status:** Implemented.

**Tests:**
`integration/test_segmentation.py::test_job_persists_explainable_segments_idempotently_and_the_endpoint_serves_them`;
`ml/tests/test_segmentation.py::test_kmeans_clustering_determinism`.

**Tasks:** TASK-044, TASK-052 (Done).

### US-32 See which customers need attention (FR-56, FR-57)

*As a relationship manager, I want customers ranked by churn risk, with signals and the top eligible product, and a
behavioural dashboard, so that I contact the right people.*

**Acceptance criteria:**

* ✓ RELATIONSHIP_MANAGER or ADMIN with MFA gets churn signals (inactivity, balance decline) that are exact on known
  ledger data, with filters and pagination.
* ✓ An RM screen exists: `/staff/insights` lists the leads filtered on the server by segment, churn risk and
  loan propensity; a customer detail shows the 30-day volume, health score, churn score and eligible products;
  calls and notes are logged server-side and audited (TASK-066, ADR-0022). An RM sees only the customers assigned
  to them; any other customer gives the same 404 as an unknown id (ADR-0032, #347).
* ✓ An ADMIN with MFA assigns, reassigns and unassigns a customer's RM from the customer flyout; each change is audited,
  the history is kept, and unassigned customers are visible to ADMIN only (ADR-0032, #347).
* ✓ A behavioural dashboard (`/staff/behaviour`, `GET /insights/behaviour`, TASK-101) shows the segment mix, spend by
  segment, product holdings and recommendation clicks, and clickstream engagement, filtered by segment and period;
  aggregates only, with a per-segment link to the RM leads (`integration/test_behaviour_analytics.py`,
  `BehaviourAnalytics.test.tsx`).
* ✓ An RM's list opens ranked by the rule-based product score (`order_by=PROPENSITY`, `rules-v1`, not a trained
  model), with the top product and its first reason on each row; churn-risk order stays available and is the
  administrator's default (ADR-0041, #391 merged in #397).

**Status:** Implemented.

**Tests** (`integration/test_insights.py`): `test_insights_rbac_authorization`,
`test_churn_signals_are_exact_on_known_ledger_data`, `test_pagination_and_filters`;
`integration/test_rm_insights_ui_api.py` (`test_loan_propensity_is_reported_and_filtered_on_the_server`,
`test_detail_has_exact_volume_health_and_products_and_no_extra_pii`, `test_new_routes_need_rm_or_admin_with_mfa`,
`test_logged_interaction_is_listed_shared_and_audited_without_the_note`); `security/test_route_policy_registry.py`
(`test_every_insights_route_is_behind_the_mfa_rm_gate`); web `RMInsights.test.tsx`, `routing.test.tsx`;
`integration/test_rm_assignment.py` (`test_an_rm_sees_only_assigned_customers_and_admin_sees_everyone`,
`test_reassignment_moves_visibility_and_keeps_the_history`, `test_concurrent_assignments_keep_exactly_one_active`),
`integration/test_rbac_bola.py::test_bola_rm_only_reaches_assigned_customers` (both orders);
`integration/test_rm_insights_ui_api.py` (`test_propensity_order_ranks_by_product_score_within_the_rm_scope`,
`test_propensity_order_combines_with_the_loan_filter`).

**Tasks:** TASK-056 (Done: #223, assignment #347, product-score order #391 merged in #397); TASK-066 (Done #277); TASK-101 (Done #325).

## UC-12 Product recommendation engine

### US-33 Recommend products from my profile (FR-53 to FR-55)

*As a retail customer, I want recommendations for loan, savings, investment and insurance products based on my
behaviour, spending, demographics and what I already hold.*

**Acceptance criteria:**

* ✓ Eligibility rules run first, then ranking.
* ✓ The catalogue covers savings, loan, investment and insurance types.
* ✓ The reasons come from my own figures.
* ✓ A high debt ratio excludes credit products.
* ✓ A consented age band is used (ADR-0015): `unknown` without a date of birth and a recorded consent, and
  withdrawal applies to the next request. The engine never receives the date.
* ✓ Products I already hold are taken into account: a held savings account, fixed deposit or (with an active
  personal loan) the credit line is not offered again, except a fixed-deposit top-up whose reason says so
  (TASK-111, ADR-0030).
* ✗ The ranking uses a trained propensity model. It is hand-weighted today; the model is deferred until real
  exposure and conversion data exist (`docs/ml/recommendation-propensity-decision.md`).

**Status:** Partial. The propensity model is TASK-054.

**Tests:**

* `ml/tests/test_recommendations.py`: `test_eligibility_exclusion_for_high_debt_ratio`,
  `test_adr_0015_age_band_never_changes_ranking_only_adds_an_eligibility_reason`;
* `integration/test_recommendations.py` (`test_engine_gets_the_age_band_only_with_consent_and_never_the_date`);
* `integration/test_demographics.py`; `apps/web/src/pages/DemographicsSection.test.tsx`;
* `ml/tests/test_recommendations_held_products.py`, `integration/test_recommendations_held_products.py`.

**Tasks:** TASK-054 (In Progress); TASK-062 (Done #328, #339); TASK-107 (Done #326); TASK-111 (Done #342).

## UC-13 Security monitoring dashboard

### US-34 Monitor security threats in one place (FR-58)

*As a fraud analyst or security officer, I want high-risk customers, active alerts, login anomalies, fraud events and
open investigations on one dashboard, so that I can monitor threats effectively.*

**Acceptance criteria:**

* ✓ A staff API (fraud analyst or admin, with MFA) and a dashboard show each of the five views: high-risk customers,
  active (open) alerts by severity with the oldest one's age, login anomalies (three or more failed sign-ins for a
  user in 24 hours, lockouts), recent flagged fraud events, and the longest-waiting open investigations, with
  drill-down to the alert queue and to each alert.
* ✓ Identifiers and aggregates only: no names, emails, account numbers or IP addresses.

**Status:** Implemented (`GET /api/v1/fraud/monitoring`, `/staff/monitoring`).

**Tests:** `test_security_monitoring.py`, `test_rbac_bola.py`, `test_route_policy_registry.py`,
`SecurityMonitoring.test.tsx`, `routing.test.tsx`, `e2e/shell.spec.ts`.

**Tasks:** TASK-099 (Done).

## Compliance (section 5)

### US-35 Inspect and verify the audit trail (FR-59)

*As a compliance officer, I want to read the audit history and verify that nobody altered it.*

**Acceptance criteria:**

* ✓ COMPLIANCE_OFFICER or ADMIN with MFA can page through the audit log.
* ✓ Verification re-computes the hash chain and pinpoints tampering.
* ✓ Rows are append-only.
* ✓ The web page shows the log, verifies the chain and highlights a break.

**Status:** Implemented.

**Tests:**

* `integration/test_audit_and_outbox.py`: `test_audit_chain_verifies_and_detects_tampering`,
  `test_audit_rows_are_append_only`, `test_compliance_audit_logs_role_and_mfa_authorization`;
* `apps/web/src/pages/staff/AuditLog.test.tsx`.

**Tasks:** TASK-048 (Done).

### US-36 Manage exceptions and regulatory indicators (FR-60)

*As a compliance officer, I want system alerts and exceptions (blocked transfers, lockouts, chain failures) that I can
acknowledge and resolve, with indicators of overdue reviews.*

**Acceptance criteria:**

* ✓ COMPLIANCE_OFFICER or ADMIN with MFA lists blocked payments, lockouts and audit-chain failures (`/staff/compliance`)
  and acknowledges or resolves each; every action is an audit row with the actor and the reason, and the status is
  read back from those rows (ADR-0027).
* ✓ Indicators: unresolved exceptions in the period, exceptions not acknowledged within 24 hours, fraud reviews open
  past 24 hours, and the audit chain status; each shown with its definition.

**Status:** Implemented (#325).

**Tests:** `integration/test_compliance_exceptions.py` (`test_exceptions_and_indicators_are_exact_on_known_events`,
`test_acknowledge_then_resolve_is_audited_with_actor_and_reason`, `test_a_tampered_audit_chain_is_an_exception_that_can_be_worked`,
`test_concurrent_acknowledgements_record_exactly_one`, `test_compliance_exceptions_need_compliance_or_admin_with_mfa`,
`test_exceptions_response_carries_no_customer_pii`); `security/test_route_policy_registry.py`
(`test_every_compliance_route_is_behind_the_mfa_compliance_gate`); web `ComplianceDashboard.test.tsx`.

**Tasks:** TASK-100 (Done #325).

## Administration (section 6)

### US-37 Manage staff users and safe runtime settings (FR-05)

*As a banking administrator, I want to create staff users with explicit roles and change the business limits that are
safe to change at runtime, with a reason that is audited, so that the bank is run from the web app without a
redeployment and without exposing secrets or security settings.*

**Acceptance criteria:**

* ✓ ADMIN with MFA creates a staff user from `/staff/admin`; no role is preselected (TASK-108).
* ✓ Only the ADR-0031 allow-list can be changed: the daily transfer limit, the per-transfer limit and the
  standing-order cap, each typed and bounded. Secrets and security settings cannot be named.
* ✓ A change applies on the next check, in the API and in the worker, without a restart.
* ✓ Every change is audited in the hash chain with the actor, the old and new value and the reason.
* ✓ Every other role gets 403 on the admin routes and a restricted page in the web app.

**Status:** Implemented (#346).

**Tests:** `integration/test_runtime_config.py` (`test_invalid_changes_are_422_and_leave_no_state`,
`test_a_lowered_daily_limit_is_enforced_on_the_next_transfer_and_bill_payment`,
`test_every_change_is_audited_with_actor_old_new_and_reason_in_the_hash_chain`,
`test_only_mfa_verified_admins_may_read_or_change_settings`); `unit/test_runtime_config_allowlist.py`;
`integration/test_scheduled_transfers_engine.py::test_the_worker_enforces_an_admins_runtime_daily_limit_without_a_restart`;
`integration/test_rbac_bola.py::test_only_admins_with_mfa_may_use_admin_routes`; web
`apps/web/src/pages/staff/Administration.test.tsx`, `routing.test.tsx`.

**Tasks:** TASK-108 (Done: #346, E2E journey #359).

---

## Summary

| Status | Stories |
|---|---|
| Implemented | US-02, US-03, US-05, US-09, US-10, US-17, US-18, US-24, US-26, US-28, US-29, US-31, US-25, US-34, US-35, US-36, US-37 |
| Partial | US-01, US-04, US-06, US-07, US-11, US-13, US-14, US-19, US-20, US-23, US-27, US-30, US-32, US-33 |
| Planned | US-08, US-12, US-15, US-16, US-21, US-22 |
