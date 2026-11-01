# PBIP user stories

| | |
|---|---|
| Task | TASK-104 (#187) |
| Derived from | *PBIP Use Case Document v0.2*, use cases UC-01 to UC-13 and the section 5 scope |
| Status checked against | `main` at `2e65b87` (1 Nov 2026) |
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
* ✗ A customer can enrol in MFA from the web app.

**Status:** Partial. The MFA enrolment screen has no task; the API exists.

**Tests:**

* `integration/test_auth.py`: `test_register_login_and_me`, `test_unknown_user_and_wrong_password_are_indistinguishable`,
  `test_progressive_lockout_and_recovery`, `test_mfa_enrolment_login_replay_protection_and_recovery_codes`,
  `test_repeated_bad_mfa_codes_lock_the_account`, `test_login_rate_limit_per_ip`;
* `apps/web/src/pages/Login.test.tsx`: "asks for the authenticator code when the server requires MFA".

**Tasks:** TASK-004, TASK-006 (Done).

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
* ✗ The administrator can do this from a screen.

**Status:** Partial. The administration UI has no task.

**Tests:** `integration/test_admin_lockout.py`; `integration/test_rbac_bola.py::test_admin_can_create_staff_and_it_is_audited`.

**Tasks:** TASK-007 (Done).

## UC-02 View banking dashboard

### US-04 See my accounts and balances (FR-07, FR-08, FR-13)

*As a retail customer, I want a consolidated view of my accounts, so that I know where my money is.*

**Acceptance criteria:**

* ✓ Each account shows its available, ledger and held balance.
* ✓ I can open a new account (up to five).
* ✗ A total balance across all accounts is shown.

**Status:** Partial. The total-balance overview has no task.

**Tests:**

* `apps/web/src/pages/Accounts.test.tsx`: "restores the session, shows balances and the amount on hold";
* `integration/test_accounts.py`: `test_open_savings_account_success`, `test_open_account_enforces_limit_of_five`;
* `apps/web/src/pages/OpenAccount.test.tsx`.

**Tasks:** TASK-005, TASK-019 (Done).

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
* ✗ A balance-forecast chart with the confidence band is shown. The API returns 80%/95% intervals labelled
  "Predicted, not exact".

**Status:** Partial. The forecast chart is TASK-050.

**Tests:**

* `apps/web/src/pages/Insights.test.tsx` (tests 3 to 11);
* `integration/test_analytics.py`: `test_spending_trends_and_mom_calculations`,
  `test_financial_health_score_moves_with_ledger_activity`,
  `test_health_score_uses_closed_months_only_so_the_calendar_day_does_not_move_it`;
* `integration/test_forecast.py::test_forecast_is_labelled_with_intervals_from_the_account_history`.

**Tasks:** TASK-040, TASK-041, TASK-042, TASK-049 (Done); TASK-050 (Todo).

### US-07 See products recommended for me (FR-12)

*As a retail customer, I want relevant product suggestions on my dashboard, so that I discover products that fit me.*

**Acceptance criteria:**

* ✓ The API returns at most three products, each with reasons built from my own figures.
* ✓ The results are scoped to the authenticated customer.
* ✗ The dashboard shows the suggestions with "why this was recommended".

**Status:** Partial. The UI is TASK-062.

**Tests:** `integration/test_recommendations.py::test_recommendations_are_scoped_to_the_authenticated_customer`,
`test_recommendations_use_the_customers_real_figures_and_ignore_date_of_birth`.

**Tasks:** TASK-054 (In Progress); TASK-062 (Todo).

## UC-03 Download account statement

### US-08 Download a PDF statement (FR-15)

*As a retail customer, I want to download a statement for an account and period as a PDF, so that I have an official
record.*

**Acceptance criteria:**

* ✗ I choose an account and a period, then click Download.
* ✗ The PDF has a bank header and the opening and closing balances.
* ✗ Only the account holder can generate it.

**Status:** Planned (`modules/statements` is empty).

**Tests:** none yet.

**Tasks:** TASK-016, TASK-074 (Todo).

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

**Status:** Implemented. Transfers have no Playwright E2E test yet (TASK-077).

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
* ✗ I can add and verify payees in the web app.

**Status:** Partial. The UI is TASK-020.

**Tests:** `integration/test_transfers.py::test_payee_name_mismatch_is_not_verified_and_never_reveals_the_real_name`;
`integration/test_rbac_bola.py::test_bola_customer_b_cannot_touch_customer_a_objects`.

**Tasks:** TASK-020 (Todo).

### US-12 Edit a payee (FR-24)

*As a retail customer, I want to correct a payee's nickname or name, so that my list stays accurate.*

**Acceptance criteria:**

* ✗ `PATCH /payees/{id}` updates an owned payee.
* ✗ A name change forces re-verification.
* ✗ The account number is immutable.
* ✗ Each edit is audited.

**Status:** Planned.

**Tests:** none yet.

**Tasks:** TASK-094 (Todo).

### US-13 Remove a payee (FR-25)

*As a retail customer, I want to delete a payee I no longer use.*

**Acceptance criteria:**

* ✓ `DELETE /payees/{id}` removes an owned payee. A non-owned payee is 404.
* ✗ I can delete a payee from the web app.

**Status:** Partial. The UI is TASK-020.

**Tests:** `integration/test_rbac_bola.py::test_bola_customer_b_cannot_touch_customer_a_objects` (delete-payee case).

**Tasks:** TASK-020 (Todo).

## Section 5 payment scope (no single use case)

### US-14 Pay a bill (FR-26)

*As a retail customer, I want to pay a utility bill from my account, so that I do not need another channel.*

**Acceptance criteria:**

* ✓ Paying a bill posts balanced entries and is idempotent.
* ✓ It counts toward the shared daily limit and is fraud-scored. STEP_UP and REVIEW hold the funds and open an
  alert, and the analyst's decision settles the payment. BLOCK declines it and opens an alert.
* ✓ Insufficient funds leave no partial state.
* ✓ A receipt and a notification are recorded.
* ✗ I can pay a bill from the web app.

**Status:** Partial. The UI is TASK-020.

**Tests** (`integration/test_bills.py`): `test_pay_bill_posts_balanced_double_entry_and_records_audit_and_notification`,
`test_idempotent_retry_returns_original_receipt_and_increments_metric`,
`test_bill_payments_respect_the_per_payment_and_shared_daily_limits`,
`test_a_flagged_bill_payment_is_held_for_review_like_a_transfer`,
`test_the_analysts_decision_settles_the_held_bill_payment`,
`test_a_blocked_bill_payment_is_declined_and_opens_an_alert`,
`test_pay_bill_insufficient_funds_leaves_zero_partial_state`.

**Tasks:** TASK-014 (Done); TASK-020 (Todo).

### US-15 Schedule a recurring transfer (FR-27)

*As a retail customer, I want standing orders, so that regular payments happen without me.*

**Acceptance criteria:**

* ✗ I can create, list and cancel schedules.
* ✗ Each occurrence runs exactly once, with key `sched-{id}-{date}`.
* ✗ Insufficient funds notify me without breaking the schedule.

**Status:** Planned.

**Tests:** none yet.

**Tasks:** TASK-015, TASK-021 (Todo).

### US-16 Search my transactions (FR-14)

*As a retail customer, I want to filter my transactions by date and amount, so that I can find a payment quickly.*

**Acceptance criteria:**

* ✗ Date and amount filters work with cursor pagination.
* ✗ The filtered query uses the account/date index.

**Status:** Planned.

**Tests:** none yet.

**Tasks:** TASK-017 (Todo).

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
* `apps/web/src/shell/NotificationBell.test.tsx`.

**Tasks:** TASK-018, TASK-022 (Done).

## UC-06 Loan recommendation

### US-19 Check what a loan would cost (FR-29)

*As a retail customer, I want to see the loan catalogue and calculate EMIs, so that I can plan a borrowing decision.*

**Acceptance criteria:**

* ✓ The catalogue lists the products.
* ✓ The EMI calculation returns an exact integer-paise schedule.
* ✗ I can use a loan calculator in the web app.

**Status:** Partial. The UI is TASK-051.

**Tests:** `integration/test_loans.py::test_catalogue_and_emi_schedule`; `unit/test_loan_emi.py::test_known_emi_values`.

**Tasks:** TASK-045 (Done); TASK-051 (Todo).

### US-20 Know whether I am eligible, and why (FR-30, FR-31)

*As a retail customer, I want an eligibility assessment based on my own finances and personalised loan suggestions, so
that I only apply for what I can afford.*

**Acceptance criteria:**

* ✓ Eligibility uses my ledger income and existing repayments, with debt-to-income at most 50%.
* ✓ The reasons show the figures.
* ✓ A recent lockout refuses the check.
* ✗ Suggestions are drawn from the loan catalogue and shown to me.

**Status:** Partial. Suggestions and their UI are TASK-054 and TASK-062. The engine currently suggests a credit line
and a rewards card.

**Tests** (`integration/test_loans.py`): `test_eligible_with_explained_figures`,
`test_rejected_when_dti_would_exceed_half_of_income`, `test_rejected_after_a_recent_lockout`.

**Tasks:** TASK-046 (Done); TASK-054 (In Progress), TASK-062 (Todo).

### US-21 Apply for and track a loan (FR-32)

*As a retail customer, I want to apply for a loan and follow its repayments.*

**Acceptance criteria:**

* ✗ An application creates a repayment schedule.
* ✗ Disbursal posts through the ledger.
* ✗ The active loan shows its remaining balance and upcoming EMIs.

**Status:** Planned.

**Tests:** none yet.

**Tasks:** TASK-047, TASK-051 (Todo).

## UC-07 Investment recommendation

### US-22 Record my risk tolerance (FR-33)

*As a retail customer, I want to answer a short questionnaire, so that suggestions match the risk I accept.*

**Acceptance criteria:**

* ✗ The questionnaire produces a band of LOW, MODERATE or HIGH.
* ✗ The profile is readable and updatable only by me.
* ✗ The assessment date and answers are kept.

**Status:** Planned.

**Tests:** none yet.

**Tasks:** TASK-097 (Todo).

### US-23 Get suitable investment suggestions (FR-34 to FR-36)

*As a retail customer, I want investment products suggested from my savings, income and available funds, and to
compare them, so that I can invest sensibly.*

**Acceptance criteria:**

* ✓ The recommendation engine suggests a fixed deposit or an index-fund SIP only when income, savings ratio, buffer
  and balance qualify.
* ✗ The suggestions use my risk tolerance.
* ✗ A wealth catalogue lists FDs, funds and bonds.
* ✗ Insurance products are listed.
* ✗ Two to four products can be compared side by side.

**Status:** Partial.

**Tests:** `ml/tests/test_recommendations.py::test_eligibility_exclusion_for_high_debt_ratio`.

**Tasks:** TASK-054 (In Progress); TASK-063, TASK-064, TASK-065, TASK-096, TASK-097, TASK-098 (Todo).

## UC-08 AI assistant interaction

### US-24 Ask about my own money in plain language (FR-37, FR-38)

*As a retail customer, I want to ask questions like "How much did I spend last month?", so that I get answers without
navigating reports.*

**Acceptance criteria:**

* ✓ Balance, recent-transaction, spending (including last month), forecast and loan-affordability-with-an-amount
  questions are answered from my own data.
* ✓ Clear questions never call the model.
* ✓ Answers stream into the chat drawer.
* ✓ If the model is down or wrong, deterministic rules answer.
* ✗ "Can I afford a personal loan?" without an amount is answered. Today it returns help text, by design: no amount is
  invented.

**Status:** Partial.

**Tests:**

* `integration/test_assistant.py`: `test_rules_engine_answers_each_tool_from_own_data`,
  `test_fallback_takes_over_when_the_model_is_down_or_wrong`,
  `test_a_loan_amount_the_customer_never_wrote_is_not_answered`;
* `unit/test_assistant_routing.py::test_clear_questions_take_the_fast_path`;
* `apps/web/src/assistant/AssistantDrawer.test.tsx`.

**Tasks:** TASK-057 to TASK-061 (Done).

### US-25 Get help, product discovery, investment ideas and unusual-expense explanations (FR-39, FR-40)

*As a retail customer, I want the assistant to answer FAQs, show suitable products, suggest investments and point out
unusual expenses.*

**Acceptance criteria:**

* ✗ The read-only tools `get_faq_answer`, `list_products` and `get_investment_suggestions` exist.
* ✗ Investment suggestions use my risk profile.
* ✗ "Show unusual expenses" returns anomalous transactions.

**Status:** Planned. The FAQ, product and investment tools are TASK-105. Unusual expenses has no task.

**Tests:** none yet.

**Tasks:** TASK-105 (Todo).

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
* ✗ Location is assessed below country level.
* ✗ The standalone `LARGE_AMOUNT` rule has a dedicated test.

**Status:** Partial.

**Tests:**

* `integration/test_transfers.py`: `test_every_scoring_decision_is_persisted_for_audit_and_retraining`,
  `test_new_payee_plus_large_amount_is_held_with_human_readable_reasons`, `test_velocity_burst_is_flagged`,
  `test_amount_spike_against_history_is_flagged`, `test_transfer_from_a_new_country_adds_a_new_country_reason`;
* `integration/test_fraud_signals.py`: `test_recent_login_failures_rule`,
  `test_velocity_falls_back_to_postgresql_when_redis_is_down`,
  `test_new_network_fires_and_the_full_ip_is_never_stored`;
* `integration/test_fraud_hybrid.py::test_any_model_problem_falls_back_to_the_rules_never_allow_all`;
* `ml/tests/test_fraud_behaviour_regression.py`.

**Tasks:** TASK-023 to TASK-034, TASK-039, TASK-080, TASK-095 (Done); #251 (closed by #257).

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

**Status:** Implemented. Charting the statistics belongs to the security dashboard (US-34, TASK-099).

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
* ✗ The web app sends events.
* ✗ Session duration is recorded.
* ✗ Product interests are derived.

**Status:** Partial.

**Tests** (`integration/test_events.py`): `test_events_batch_happy_path`,
`test_events_reject_extra_fields_free_text_and_urls`, `test_no_ip_address_or_user_agent_is_stored`,
`test_events_partition_routing_across_months`.

**Tasks:** TASK-055 (In Progress); TASK-101 (Todo).

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
* ✗ An RM screen exists.
* ✗ A behavioural dashboard shows segments, affinity and engagement.

**Status:** Partial.

**Tests** (`integration/test_insights.py`): `test_insights_rbac_authorization`,
`test_churn_signals_are_exact_on_known_ledger_data`, `test_pagination_and_filters`.

**Tasks:** TASK-056 (In Progress); TASK-066, TASK-101 (Todo).

## UC-12 Product recommendation engine

### US-33 Recommend products from my profile (FR-53 to FR-55)

*As a retail customer, I want recommendations for loan, savings, investment and insurance products based on my
behaviour, spending, demographics and what I already hold.*

**Acceptance criteria:**

* ✓ Eligibility rules run first, then ranking.
* ✓ The catalogue covers savings, loan, investment and insurance types.
* ✓ The reasons come from my own figures.
* ✓ A high debt ratio excludes credit products.
* ✗ A consented age band is used. It is always `unknown` today (ADR-0015).
* ✗ Products I already hold are taken into account.
* ✗ The ranking uses a trained propensity model. It is hand-weighted today.

**Status:** Partial. The age band is TASK-107. Existing holdings have no task. The propensity model is TASK-054.

**Tests:**

* `ml/tests/test_recommendations.py`: `test_eligibility_exclusion_for_high_debt_ratio`,
  `test_adr_0015_age_band_never_changes_ranking_only_adds_an_eligibility_reason`;
* `integration/test_recommendations.py`.

**Tasks:** TASK-054 (In Progress); TASK-062, TASK-107 (Todo).

## UC-13 Security monitoring dashboard

### US-34 Monitor security threats in one place (FR-58)

*As a fraud analyst or security officer, I want high-risk customers, active alerts, login anomalies, fraud events and
open investigations on one dashboard, so that I can monitor threats effectively.*

**Acceptance criteria:**

* ✗ A staff API (with MFA) and a dashboard show each of the five panels, with drill-down to the alert queue.

**Status:** Planned. Partial data exists elsewhere: the fraud alert queue and statistics API, and the Grafana
panels for fraud decisions and failed logins.

**Tests:** none for the dashboard.

**Tasks:** TASK-099 (Todo).

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

* ✗ An exceptions list with acknowledge and resolve actions, each audited.
* ✗ SLA and unresolved-count indicators.

**Status:** Planned.

**Tests:** none yet.

**Tasks:** TASK-100 (Todo).

---

## Summary

| Status | Stories |
|---|---|
| Implemented | US-02, US-05, US-09, US-10, US-17, US-18, US-26, US-28, US-29, US-31, US-35 |
| Partial | US-01, US-03, US-04, US-06, US-07, US-11, US-13, US-14, US-19, US-20, US-23, US-24, US-27, US-30, US-32, US-33 |
| Planned | US-08, US-12, US-15, US-16, US-21, US-22, US-25, US-34, US-36 |
