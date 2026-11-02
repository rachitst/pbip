# Requirements traceability: use case document v0.2 vs. tasks, code and tests

Source of truth: *Personal Banking Intelligence Platform (PBIP) Use Case Document v0.2*
(`Final Year Banking App Project Use Case_v0.2 1 (1).docx`, the only copy found; sections 5, 7 to 11).
This file records, for every requirement, the backlog task(s) and issue(s) that own it, what the code
on `main` actually does, and which CI tests prove it. Update it whenever a feature PR merges.

Status values:

* **Verified**: implemented, and an automated test that runs in CI proves the behaviour.
* **Partial**: part of the requirement is implemented and verified; the rest is missing (stated).
* **Unverified**: implemented or configured, but never exercised by a test or a real environment.
* **Missing**: not implemented.

Task status comes from `docs/TEAM_EXECUTION_PLAN.md` and the project board.
TASK-094 to TASK-107 were created on 2026-10-30 for requirements no task covered; they are
unassigned, without dates, and Todo until someone starts one (capacity confirmed by the product owner).

Last reviewed against `main` on 2026-11-01 (`4fa96df`) for TASK-069/078/079/083/090/102/103 (Session 4); earlier (`2e65b87`) for TASK-104; the earlier local verification run is
recorded in `docs/testing/local-verification-2026-10-30.md`. The numbered requirements (FR/NFR) are in
[requirements-specification.md](requirements-specification.md) and the user stories, with acceptance criteria and
the tests that prove them, in [user-stories.md](user-stories.md).

## Functional use cases (section 7)

| Use case | Status | What exists on `main` (code, tests) | Owning tasks (issue, status) | Gap |
|---|---|---|---|---|
| UC-01 Customer login: credentials, MFA, failed-login events | Verified | `modules/auth/`, `pages/Login.tsx` (incl. MFA code step); `test_auth.py`, `test_admin_lockout.py`, `test_passwords.py`, `test_ratelimit.py`, E2E `sprint1.spec.ts` | TASK-004 #15, 006 #17, 007 #18 (Done); #201 | MFA optional for customers (mandatory for staff); no web registration or MFA-enrolment screens yet (APIs exist; #201) |
| UC-02 Dashboard: balance, recent transactions, spending insights, recommendations | Partial | Dashboard shell from TASK-019 (#150): navigation, light/dark theme, per-account available/ledger/held balances, paginated recent transactions, quick actions (open account, send money) in `pages/Accounts.tsx`; spending, trends and health score UI in `pages/Insights.tsx` (TASK-049); `Accounts.test.tsx`, `Insights.test.tsx`, E2E | 019 #42, 049 #72 (Done); 054 #77 (In Progress); 050 #73, 062 #85 (Todo) | No total-balance overview across accounts (no task); forecast has an API but no chart (TASK-050); recommendations API exists (TASK-054) but no UI (TASK-062) |
| UC-03 Download statement (PDF) | Missing | `modules/statements/` empty | 016 #39, 074 #97 (Todo) | Whole use case |
| UC-04 Transfer money: source, payee, amount, review, confirm; balance, fraud, limits | Partial | API: `modules/payments/`, ledger, fraud scoring, limits, idempotency, TOTP step-up (`test_transfers.py`, `test_ledger*.py`); UI: "Send money" on the accounts page with source-account choice, review/confirm dialog, TOTP step-up modal and outcome messages from the real response; three wrong codes escalate to analyst review server-side (`SendMoney.test.tsx`, `test_three_wrong_step_up_codes_hand_the_transfer_to_an_analyst`) | 037 #60 (Done) | Transfers have no Playwright E2E test |
| UC-05 Manage payees: add, edit, delete, verify | Partial | API add, list, get, delete, verify (`modules/payees/`, BOLA matrix); UI lists payees in the send-money form only | 020 #43 (Todo), **094 #177 (new)** | No edit endpoint (TASK-094); no add/verify/delete UI (TASK-020) |
| UC-06 Loan recommendation | Partial | `modules/loans/`: catalogue, EMI schedule (integer paise, Hypothesis properties), explainable eligibility from ledger income and repayments (DTI <= 50%, recent lockout refuses); `test_loans.py`, `test_loan_emi.py` | 045 #68, 046 #69 (Done); 047 #70, 051 #74 (Todo); 054 #77 (In Progress) | Eligibility uses income and EMIs, not balances or spending; the recommendation engine suggests a credit line and a rewards card, not the catalogue loans; no loan UI, applications or tracking |
| UC-07 Investment recommendation (risk tolerance, savings, income, funds) | Partial | `GET /api/v1/recommendations` (TASK-054, `rules-v1`) can suggest a fixed deposit or an index-fund SIP from income, savings ratio, buffer and balance (`pbip_ml/recommendations/engine.py`; `ml/tests/test_recommendations.py`); `modules/wealth/` is empty | 054 #77 (In Progress); 063 #86, 064 #87, 065 #88, 097 #180 (Todo) | No risk tolerance (TASK-097), no wealth catalogue, booking or UI |
| UC-08 AI assistant | Partial (API verified) | `POST /api/v1/assistant/chat` and `/chat/stream` (SSE): five read-only tools (balance, transactions, spend, forecast, loan affordability), rules-first routing (clear questions never call the model: p95 under 0.1 s), Ollama router (qwen2.5:1.5b default) for unclear questions with rules fallback, refusals, input sanitiser and output filter, redacted history, audit, adversarial suite (`test_assistant.py`, `test_assistant_adversarial.py`) | 057 #80, 059 #82, 060 #83, 067 #90 (Done), 061 #84 (Done: chat drawer on every customer page), 076 #99 (Done: `docs/security/ai-assistant-security.md`), 058 #81 (Done: routed p95 82-87 ms; LLM path 12-50 s on the reference laptop, documented), **105 #188 (new)** | FAQ/support, product discovery and investment suggestions had no tool (TASK-105) |
| UC-09 Fraud detection triggers | Partial | `pbip_ml/fraud/features.py` (TASK-024: offline/online parity-tested features) and `pbip_ml/fraud/rules.py` (TASK-025: weighted rule registry incl. RAPID_ATO_TRANSFER), LightGBM and Isolation Forest trained on the labelled data (TASK-029/030), hybrid combiner with SHAP reasons (TASK-031/032; `docs/ml/fraud-models.md`), **served** on transfers and bills behind `FRAUD_SCORER=hybrid` with the baseline as a floor and rules-only fail-safe (TASK-033, ADR-0019; `test_fraud_hybrid.py`; served p95 24.7 ms), `hybrid` in local/demo since #251 (generator v2.1: legitimate new payees held 0.09% at REVIEW; recall 0.835; evaluation report v1.1), code default `rules`; `fraud/baseline.py` (`baseline-rules-0.3`): LARGE_AMOUNT, VELOCITY_BURST (Redis window, PostgreSQL fallback), AMOUNT_SPIKE, NEW_PAYEE, NEW_PAYEE_LARGE_AMOUNT, **NEW_COUNTRY**, RECENT_LOGIN_FAILURES, NEW_DEVICE_OR_NETWORK (DB-IP Lite country, ADR-0014; approximate, signal only); score and reasons persisted; `test_transfers.py`, `test_geoip.py`. Labelled data for training: data-gen v2 injects F1-F6 with a label file (`pbip_datagen/fraud.py`, TASK-023); v2.2 adds F7 structuring (deposits just below the INR 1,00,000 threshold) and F8 dormant-account reactivation (maximum transfers more than 180 days after the last activity), additive on their own random stream (`data-gen/tests/test_fraud.py`: `test_v2_1_rows_are_unchanged`, F7/F8 signature tests; TASK-071). F11 circular transfers / mule rings: offline spike only, not served (TASK-072: `pbip_ml/fraud/circular.py`, `test_fraud_circular.py`, `docs/ml/circular-transfer-spike.md`) | 072 #95 (In Review: spike), 023 #46, 071 #94 (In Review: #261), 025 #48, 029-034 #52-57 (Done), 039 #62 (Done: `docs/ml/model-evaluation-report-v1.md`), 080 #103 (Done: behaviour-regression suite), 095 #178 (Done: RECENT_LOGIN_FAILURES, NEW_DEVICE_OR_NETWORK, ADR-0020) | Login-failure and new device/network rules and Redis velocity windows done (TASK-095/034, `baseline-rules-0.3`); location accuracy unevaluated (no labelled data) |
| UC-10 Fraud alert management: alert, customer notified, analyst review | Verified | Alerts API with MFA-gated decision; customer `TRANSFER_UNDER_REVIEW` / declined / posted notifications via outbox; `test_transfers.py`, `test_notifications.py` | 022 #45 (Done: notification bell and tray), 035 #58 (Done: `GET /api/v1/fraud/stats`), 036 #59 (Done: `/staff/fraud` queue and alert detail with release/reject; `Fraud.test.tsx`) | Fraud statistics are not charted in the UI (not covered by TASK-099; follow-up needed) |
| UC-11 Behaviour analytics: page visits, clickstream, session, product interests, spend and income | Partial | Spend patterns (`/analytics/spending`, `/trends`), RFM segmentation job (weekly Celery beat run, TASK-052); nightly 02:00 materialisation; clickstream ingestion `POST /api/v1/events/batch` into monthly partitions (TASK-055); `test_analytics.py`, `test_segmentation.py`, `test_events.py` | 040 #63, 044 #67, 052 #75 (Done); 055 #78, 056 #79 (In Progress); 101 #184 (Todo) | The web app sends no events; no session duration; no product-interest data |
| UC-12 Product recommendation engine (demographics, behaviour, spend, existing products) | Partial | `GET /api/v1/recommendations`: eligibility rules then hand-weighted ranking over savings, loan, investment and insurance products, reasons from the customer's own figures, segment as input (`rules-v1`); `test_recommendations.py` (API and ML) | 054 #77 (In Progress); 062 #85, 107 #194 (Todo) | No UI (TASK-062); age band always `unknown` until TASK-107 (ADR-0015); existing products not an input (no task); no trained propensity model |
| UC-13 Security monitoring dashboard | Verified | `GET /api/v1/fraud/monitoring` (`fraud/monitoring.py`, analyst/admin + MFA, 60 s Redis cache, ids and aggregates only): high-risk customers, open alerts by severity with oldest age, login anomalies (failed-sign-in bursts, lockouts), latest flagged fraud events with alert ids; `/staff/monitoring` page with drill-down to `/staff/fraud/{id}`; `test_security_monitoring.py`, `test_rbac_bola.py`, `test_route_policy_registry.py`, `SecurityMonitoring.test.tsx`, `routing.test.tsx`, `e2e/shell.spec.ts` | 035 #58 (Done), 099 #182 (Done) | Fraud statistics (`/fraud/stats`) are still not charted in the UI |

## Scope items (section 5)

| Item | Status | Owning tasks | Gap |
|---|---|---|---|
| Financial health indicators | Partial | 041 #64 (Done), 042 #65 (Done: `GET /api/v1/analytics/forecast`, balance forecast with 80/95% intervals labelled "Predicted, not exact", `test_forecast.py`), 049 #72 (Done: health-score UI), 050 #73 (Todo) | No forecast chart (TASK-050) |
| Account details, transaction search, statements, settings | Partial | 005, 008 (Done); 017 #40, 016 #39, 074 #97 (Todo) | Search/filter (TASK-017) and statements missing; profile, devices, password change verified (`Profile.tsx`, `test_customers.py`, E2E) |
| Bill payments, scheduled transfers | Partial (API verified) | 014 #37 (Done: `POST /api/v1/bills/pay`, idempotent double-entry posting, fraud scoring with the same outcomes as transfers (REVIEW and STEP_UP hold the funds for an analyst, whose decision settles the bill; BLOCK declines), daily limit shared with transfers, `test_bills.py`); 015 #38, 020 #43, 021 #44 (Todo) | No bill-pay UI (TASK-020); scheduled transfers missing (TASK-015, TASK-021) |
| Loans: catalogue, eligibility, EMI calculator, applications, tracking | Partial | catalogue, EMI calculator, eligibility: 045, 046 (Done) | applications and tracking: 047, 051 (Todo) |
| Wealth: fixed deposits, mutual funds, stocks and bonds | Missing | 063, 064, 065 (Todo) | |
| Wealth: insurance products | Missing | **096 #179 (new)** | No task covered insurance |
| Wealth: product comparison | Missing | **098 #181 (new)** | No task covered comparison |
| Fraud monitoring dashboard | Partial (API verified) | 035 #58 (Done: `GET /api/v1/fraud/stats`, analyst+MFA, 60 s Redis cache, `test_fraud_stats.py`), 036 #59 (Done: `/staff/fraud` queue and alert detail with plain-language reasons and SHAP bars, release/reject) | Stats charts not in the UI yet (the TASK-099 security dashboard shows flagged events and alerts, not the `/fraud/stats` trends; follow-up needed) |
| Behavioural analytics dashboard: segmentation, spend patterns, product affinity, engagement | Partial (segmentation and RM insights APIs) | 044 (Done), 056 #79 (In Progress: `GET /api/v1/insights/customers`), 066 #89, 101 #184 (Todo) | No dashboard UI; product affinity and engagement metrics (TASK-101) |
| Compliance and risk dashboard: audit history, system alerts, exceptions, regulatory monitoring | Partial (audit API and UI) | 048 #71 (Done: audit log page with chain verification), 100 #183 (Todo) | Alerts, exception management and monitoring (TASK-100) |

## Non-functional requirements (section 8)

| Requirement | Status | Evidence | Owning tasks | Gap |
|---|---|---|---|---|
| Secure authentication | Verified | auth tests, route-policy registry test | 004, 006, 007 (Done); 090 #113 | |
| Encrypt sensitive data | Verified | `core/crypto.py`, `test_crypto.py`, `test_rotate_field_key.py`; TLS in the repository (#280): nginx TLS 1.2+ with HSTS, PostgreSQL `verify-full`, proven by `scripts/verify_tls.sh` in the CI e2e job | 075 (Done) | Certbot issuance on a real staging host (#198) |
| Role-based access control | Verified | `require_roles`, `test_rbac_bola.py` | | |
| Audit logs | Verified | hash-chained `audit_logs`, `test_audit_and_outbox.py`, `AuditLog.test.tsx` | 048 #71 (Done) | |
| OWASP Top 10 | Partial | threat model mapping, Bandit (0 high/medium), CodeQL, nightly fuzzing, OWASP ZAP baseline against the running HTTPS stack (0 WARN/0 FAIL after the CORP/COEP fix, nightly `dast.yml`), `docs/security/security-audit-2026-11.md` | 028, 070, 090 (Done, #290) | ZAP baseline is passive; no authenticated active scan; no DAST against a deployed environment (#198) |
| Page load < 2 s, API < 500 ms, login < 3 s | Partial | Laptop, 2-CPU Celeron ([performance report](../testing/performance-2026-11-01.md)): browser page loads 913-1,665 ms and login 2,102 ms (met, `e2e/perf.spec.ts`); API p95 1.5-5.0 s at 50 VUs through nginx TLS (not met on the laptop); k6 suite `infra/k6/load.js` and nightly `perf.yml` | 078 #101, 102 #185, 079 #102 (In Progress, #288) | API budget unproven on staging hardware (#198) |
| 99% availability in test environment | Partial | Blackbox probes (web-ready through nginx TLS, api-ready, api-live), `pbip:availability:ratio_30d`, `AvailabilityBelow99` alert, verified on the laptop stack | 103 #186 (In Progress, #289) | No deployed test environment to measure a month on (#198) |
| Horizontal scaling, stateless APIs, containers | Partial | Docker images, compose files, JWT access tokens | 082 (Done), 079 #102 | Multi-replica deployment never exercised |
| Centralised logging, error tracking, health, metrics | Verified (locally) | Loki/Alloy with `service` labels; `unhandled_exception` events (request id, route template, no message) and `unhandled_errors_total`, `test_errors.py`; dashboards and alerts as code, `test_monitoring_as_code.py`; all panels rendered data on the running stack | 069 #92 (Done, #289), 103 #186 | Not deployed; no alert notification channel (owner decision) |

## Deliverables and engineering practices (sections 9 to 11)

| Item | Status | Owning tasks | Gap |
|---|---|---|---|
| Responsive web application | Partial | 019 (Done) and the Todo UI tasks | Most journeys have no UI |
| Backend API services, database | Verified | | 57 operations, migrations 0001-0010 |
| AI assistant | Partial | 057-061, 067, 076, 105 | Chat drawer UI, streaming API, adversarial suite, security architecture document; LLM latency target unmet on CPU-only hardware |
| Fraud monitoring dashboard, reporting dashboard | Partial | 035, 036, 048, 049 (Done); 066, 099, 100, 101 (Todo) | Fraud alert queue and detail, customer insights and audit log pages exist; no charted fraud/security, RM, compliance-exception or behavioural dashboard |
| API documentation | Verified | | OpenAPI drift-checked in CI |
| Automated tests, >= 70% coverage | Verified (backend) | 087 #110 | Backend 96% in CI and 95.99% in a local run against PostgreSQL ([local verification](../testing/local-verification-2026-10-30.md)); frontend threshold not traced here |
| UI testing of critical journeys | Partial | 077 #100 | Transfers, payees, statements lack E2E |
| CI: build, test, quality scan, security scan | Verified | | |
| CI: package, deploy, smoke test | Unverified | 082 (Done), #198 | Deploy gated off; never run against a host (no staging host exists; first deployment tracked in #198) |
| Deployment scripts, monitoring configuration | Unverified | 082 (Done), 069 #92, #198 | Not deployed |
| Requirements specification, user stories | Verified | 104 #187 | [requirements-specification.md](requirements-specification.md), [user-stories.md](user-stories.md) |
| HLD, LLD, ER diagram | Partial | 053 #76 (Done: [HLD](../architecture/HLD.md)), 106 #189 (Done: [ER diagram](../architecture/er-diagram.md), generated from the migrated schema, drift-checked in CI), 027 #50 (Todo) | LLD (TASK-027, waits for TASK-015) |
| Deployment guide, support guide | Verified | 013, 075 (Done) | |
| User manual | Missing | 092 #115 | |
| Peer reviews | Partial | | Ruleset requires 0 approvals; by product-owner decision (2026-10-30) completed tasks are not reopened only because a fix lacks an independent review |

## Scope conflicts (decisions needed, not guessed)

1. **"New geographic location" (UC-09) (resolved 2026-10-30)**: the product owner approved it; ADR-0014
   selects DB-IP Lite (CC BY 4.0, country level) and records accuracy, privacy and spoofing limits.
2. **"Demographics" (UC-12) (resolved 2026-10-30)**: the product owner authorised demographics; ADR-0015
   limits them to an optional, consented date of birth used only as a derived age band for eligibility,
   with no inference or backfill for existing customers (TASK-107 #194).
3. **TASK-052 vs. TASK-040 (resolved 2026-10-30)**: TASK-052's criteria were clarified onto the
   existing job instead of adding a second one: one nightly job at 02:00 UTC writing
   `monthly_category_spending`, with duration and account counts logged and a 100-account test, plus a
   weekly segmentation run (ADR-0011 addendum, ADR-0012).
4. **Delivery capacity**: section 1 of the plan already shows the Nov 3 deadline needs a 3.4x speed-up;
   the 13 new tasks are unscheduled until the team applies the slip order.

## Most important gaps (by project impact)

1. **AI assistant (UC-08)**: success criterion and deliverable; built end to end (API, hardening, streaming, chat drawer: TASK-057–061, 067); routed questions answer in under 0.1 s; the LLM path for unclear questions is 12-50 s on the reference laptop (documented, ADR-0018 amendment).
2. **Recommendations (UC-06, 07, 12)**: success criterion "recommendations generated using
   behavioural data" is met only at the API (`rules-v1`, segment and spending inputs; TASK-054); no UI
   (TASK-062), no risk tolerance (TASK-097), the wealth module is empty, and loan applications are missing.
3. **Dashboards**: the customer spending/health page (TASK-049), the fraud alert queue and detail
   (TASK-036) and the audit log page (TASK-048) and the security monitoring page (TASK-099) exist; compliance exceptions
   (TASK-100), RM (TASK-066) and behavioural (TASK-101) dashboards do not; success criterion "reporting dashboards provide meaningful insights".
4. **Statements PDF (UC-03), bill payments and scheduled transfers**.
5. **NFR evidence**: performance, availability, deployment never exercised against real targets.
