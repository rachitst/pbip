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

Last reviewed against `main` on 2026-10-30 (local verification run recorded in
`docs/testing/local-verification-2026-10-30.md`).

## Functional use cases (section 7)

| Use case | Status | What exists on `main` (code, tests) | Owning tasks (issue, status) | Gap |
|---|---|---|---|---|
| UC-01 Customer login: credentials, MFA, failed-login events | Verified | `modules/auth/`, `pages/Login.tsx` (incl. MFA code step); `test_auth.py`, `test_admin_lockout.py`, `test_passwords.py`, `test_ratelimit.py`, E2E `sprint1.spec.ts` | TASK-004 #15, 006 #17, 007 #18 (Done); #201 | MFA optional for customers (mandatory for staff); no web registration or MFA-enrolment screens yet (APIs exist; #201) |
| UC-02 Dashboard: balance, recent transactions, spending insights, recommendations | Partial | Dashboard shell from TASK-019 (#150): navigation, light/dark theme, per-account available/ledger/held balances, paginated recent transactions, quick actions (open account, send money) in `pages/Accounts.tsx`; `Accounts.test.tsx`, E2E | 019 #42 (Done); 049 #72, 062 #85, 054 #77 (Todo) | No total-balance overview across accounts; spending insights and health score have APIs (TASK-040/041) but no UI (TASK-049); no recommendations (TASK-054/062) |
| UC-03 Download statement (PDF) | Missing | `modules/statements/` empty | 016 #39, 074 #97 (Todo) | Whole use case |
| UC-04 Transfer money: source, payee, amount, review, confirm; balance, fraud, limits | Partial | API: `modules/payments/`, ledger, fraud scoring, limits, idempotency, TOTP step-up (`test_transfers.py`, `test_ledger*.py`); UI: "Send money" form on the accounts page | 037 #60 (Todo) | No review/confirm step and no step-up modal in the UI; transfers have no E2E test |
| UC-05 Manage payees: add, edit, delete, verify | Partial | API add, list, get, delete, verify (`modules/payees/`, BOLA matrix); UI lists payees in the send-money form only | 020 #43 (Todo), **094 #177 (new)** | No edit endpoint (TASK-094); no add/verify/delete UI (TASK-020) |
| UC-06 Loan recommendation | Partial | `modules/loans/`: catalogue, EMI schedule (integer paise, Hypothesis properties), explainable eligibility from ledger income and repayments (DTI <= 50%, recent lockout refuses); `test_loans.py`, `test_loan_emi.py` | 045 #68, 046 #69 (Done); 047 #70, 051 #74 (Todo); 054 #77 (In Progress) | Loan suggestions on the dashboard, applications and tracking |
| UC-07 Investment recommendation (risk tolerance, savings, income, funds) | Missing | `modules/wealth/`, `modules/recommendations/` empty | 054 #77, 063 #86, 064 #87, 065 #88 (Todo), **097 #180 (new)** | Risk tolerance had no task (TASK-097) |
| UC-08 AI assistant | Missing | `modules/assistant/` empty | 057 #80, 058 #81, 059 #82, 060 #83, 061 #84, 067 #90, 076 #99 (Todo), **105 #188 (new)** | FAQ/support, product discovery and investment suggestions had no tool (TASK-105) |
| UC-09 Fraud detection triggers | Partial | `fraud/baseline.py` (`baseline-rules-0.2`): LARGE_AMOUNT, VELOCITY_BURST, AMOUNT_SPIKE, NEW_PAYEE, NEW_PAYEE_LARGE_AMOUNT, **NEW_COUNTRY** (DB-IP Lite country, ADR-0014; approximate, signal only); score and reasons persisted; `test_transfers.py`, `test_geoip.py`. Labelled data for training: data-gen v2 injects F1-F6 with a label file (`pbip_datagen/fraud.py`, TASK-023) | 023 #46, 025 #48, 029-034 #52-57 (Todo), 095 #178 (In Progress) | No rule for login failures or new device/network (TASK-095); location accuracy unevaluated (no labelled data) |
| UC-10 Fraud alert management: alert, customer notified, analyst review | Partial | Alerts API with MFA-gated decision; customer `TRANSFER_UNDER_REVIEW` / declined / posted notifications via outbox; `test_transfers.py`, `test_notifications.py` | 036 #59, 022 #45 (Todo) | No analyst UI (TASK-036); no notification tray (TASK-022) |
| UC-11 Behaviour analytics: page visits, clickstream, session, product interests, spend and income | Partial | Spend patterns (`/analytics/spending`, `/trends`), RFM segmentation job (weekly Celery beat run, TASK-052); nightly 02:00 materialisation; both run by a real Celery worker locally; `test_analytics.py`, `test_segmentation.py` | 040 #63, 044 #67, 052 #75 (Done); 055 #78, 056 #79 (Todo) | No clickstream/session capture (TASK-055); no product-interest data |
| UC-12 Product recommendation engine (demographics, behaviour, spend, existing products) | Missing | Segmentation exists as an input | 054 #77, 062 #85 (Todo), **107 #194 (new)** | Whole use case; demographics limited to a consented age band (ADR-0015, TASK-107) |
| UC-13 Security monitoring dashboard | Partial | Audit log API with hash-chain verification; Grafana/Prometheus config | 035 #58 (Todo), **099 #182 (new)** | No security view of high-risk customers, login anomalies, open investigations (TASK-099) |

## Scope items (section 5)

| Item | Status | Owning tasks | Gap |
|---|---|---|---|
| Financial health indicators | Partial (API verified) | 041 #64 (Done), 042 #65 (Done: `GET /api/v1/analytics/forecast`, balance forecast with 80/95% intervals labelled "Predicted, not exact", `test_forecast.py`), 049 #72 (Todo) | No UI for the health score or the forecast |
| Account details, transaction search, statements, settings | Partial | 005, 008 (Done); 017 #40, 016 #39, 074 #97 (Todo) | Search/filter (TASK-017) and statements missing; profile, devices, password change verified (`Profile.tsx`, `test_customers.py`, E2E) |
| Bill payments, scheduled transfers | Partial (API verified) | 014 #37 (Done: `POST /api/v1/bills/pay`, idempotent double-entry posting, fraud scoring, daily limit shared with transfers, `test_bills.py`); 015 #38, 020 #43, 021 #44 (Todo) | No bill-pay UI; scheduled transfers missing; a fraud-flagged bill payment is declined rather than held for review |
| Loans: catalogue, eligibility, EMI calculator, applications, tracking | Partial | catalogue, EMI calculator, eligibility: 045, 046 (Done) | applications and tracking: 047, 051 (Todo) |
| Wealth: fixed deposits, mutual funds, stocks and bonds | Missing | 063, 064, 065 (Todo) | |
| Wealth: insurance products | Missing | **096 #179 (new)** | No task covered insurance |
| Wealth: product comparison | Missing | **098 #181 (new)** | No task covered comparison |
| Fraud monitoring dashboard | Missing (UI) | 035 #58, 036 #59 (Todo) | |
| Behavioural analytics dashboard: segmentation, spend patterns, product affinity, engagement | Partial (segmentation API) | 044 (Done), 056 #79, 066 #89 (Todo), **101 #184 (new)** | Product affinity and engagement metrics had no task |
| Compliance and risk dashboard: audit history, system alerts, exceptions, regulatory monitoring | Partial (audit API) | 048 #71 (Todo), **100 #183 (new)** | Alerts, exception management and monitoring had no task |

## Non-functional requirements (section 8)

| Requirement | Status | Evidence | Owning tasks | Gap |
|---|---|---|---|---|
| Secure authentication | Verified | auth tests, route-policy registry test | 004, 006, 007 (Done); 090 #113 | |
| Encrypt sensitive data | Verified | `core/crypto.py`, `test_crypto.py`, `test_rotate_field_key.py` | 075 (Done) | TLS not configured in the repo (runbook section 1.4) |
| Role-based access control | Verified | `require_roles`, `test_rbac_bola.py` | | |
| Audit logs | Verified | hash-chained `audit_logs`, `test_audit_and_outbox.py` | 048 #71 (UI) | |
| OWASP Top 10 | Partial | threat model mapping, Bandit, CodeQL, nightly fuzzing (run 40948518916: 940 cases, 0 5xx) | 028 (Done), 070 (Done), 090 #113 | No DAST against a deployed environment |
| Page load < 2 s, API < 500 ms, login < 3 s | Partial | Analytics endpoints measured locally with `scripts/measure_analytics_latency.py`: p95 12-19 ms, 300 requests each ([local verification](../testing/local-verification-2026-10-30.md)); `infra/k6/baseline.js` (TASK-012) | 078 #101, **102 #185 (new)** | Page load and login never measured; other routes not measured; nothing measured under concurrency (078 #101) |
| 99% availability in test environment | Unverified | | **103 #186 (new)** | No environment deployed; no task existed |
| Horizontal scaling, stateless APIs, containers | Partial | Docker images, compose files, JWT access tokens | 082 (Done), 079 #102 | Multi-replica deployment never exercised |
| Centralised logging, error tracking, health, metrics | Unverified | `infra/{loki,alloy,prometheus,grafana}`, `/api/health/*` | 069 #92, 085 #108, **103 #186 (new)** | Error tracking had no task; nothing deployed |

## Deliverables and engineering practices (sections 9 to 11)

| Item | Status | Owning tasks | Gap |
|---|---|---|---|
| Responsive web application | Partial | 019 (Done) and the Todo UI tasks | Most journeys have no UI |
| Backend API services, database | Verified | | 38 operations, migrations 0001-0005 |
| AI assistant | Missing | 057-061, 067, 076, 105 | |
| Fraud monitoring dashboard, reporting dashboard | Missing | 036, 049, 066, 099, 100, 101 | |
| API documentation | Verified | | OpenAPI drift-checked in CI |
| Automated tests, >= 70% coverage | Verified (backend) | 087 #110 | Backend 96% in CI and 95.99% in a local run against PostgreSQL ([local verification](../testing/local-verification-2026-10-30.md)); frontend threshold not traced here |
| UI testing of critical journeys | Partial | 077 #100 | Transfers, payees, statements lack E2E |
| CI: build, test, quality scan, security scan | Verified | | |
| CI: package, deploy, smoke test | Unverified | 082 (Done), #198 | Deploy gated off; never run against a host (no staging host exists; first deployment tracked in #198) |
| Deployment scripts, monitoring configuration | Unverified | 082 (Done), 069 #92, #198 | Not deployed |
| Requirements specification, user stories | Missing | **104 #187 (new)** | No task existed |
| HLD, LLD, ER diagram | Missing | 053 #76, 027 #50, **106 #189 (new)** | ER diagram had no explicit task |
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

1. **AI assistant (UC-08)**: success criterion and deliverable; nothing implemented.
2. **Recommendations (UC-06, 07, 12)**: success criterion "recommendations generated using
   behavioural data" is unmet; loans and wealth modules are empty.
3. **Dashboards**: spending/health UI (TASK-049), fraud operations (TASK-036), security, compliance
   and behavioural dashboards; success criterion "reporting dashboards provide meaningful insights".
4. **Statements PDF (UC-03), bill payments and scheduled transfers**.
5. **NFR evidence**: performance, availability, deployment never exercised against real targets.
