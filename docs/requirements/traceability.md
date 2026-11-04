# Requirements traceability: use case document v0.2 vs. code and tests

Source of truth: *Personal Banking Intelligence Platform (PBIP) Use Case Document v0.2*, sections 5 (scope), 7
(UC-01 to UC-13), 8 (non-functional), 9 (deliverables), 10 (engineering practices) and 11 (success criteria). This
file records, for every requirement, what the code does, which automated tests prove it, and what is missing.

**Checked against** `main` at `775ec1a` (5 Nov 2026), which includes #461, #462, #463, #464, #466, #467, #468, #469,
#470, #472, #473 and #477, plus the pull requests still in review: #474 (assistant FAQ for every feature and insurance
discovery, in batch L #481) and the wealth scope: investing API (#465), insurance and web (PR pending), fund SIPs (PR
pending). Evidence that exists only in one of those PRs names it.

Status values:

* **Verified**: implemented, and an automated test that runs in CI proves it.
* **Partial**: part is implemented and verified; the rest is stated in the Gap column.
* **Unverified**: implemented or configured, but not yet exercised against a real deployed environment.
* **Missing**: not implemented.

The numbered requirements (FR, NFR, ER) are in [requirements-specification.md](requirements-specification.md); each use
case's flows, screens and tests are in [use-cases.md](use-cases.md); acceptance criteria per story are in
[user-stories.md](user-stories.md). Test paths are relative to `apps/api/tests/`, `apps/web/src/`, `apps/web/e2e/`
and `ml/tests/`.

## Functional use cases (section 7)

| Use case | Status | Code | Tests that prove it | Gap |
|---|---|---|---|---|
| UC-01 Customer login | Verified | `modules/auth/` (Argon2id, TOTP with recovery codes, progressive lockout, rate limits, rotating refresh with reuse detection, token-version check, ADR-0021); `Login.tsx`, `Register.tsx`, `MfaEnrolment.tsx` | `integration/test_auth.py`, `test_access_token_revocation.py`, `test_admin_lockout.py`, `test_ratelimit.py`; E2E `journey-onboarding.spec.ts` | MFA optional for customers, mandatory for staff (by design) |
| UC-02 Banking dashboard | Verified | `/accounts` (balances, total per currency, recent transactions, recommendation carousel), `/insights` (spending, trends, health score, forecast) | `test_accounts.py`, `test_account_summary.py`, `test_analytics.py`, `test_forecast_projection.py`; `Accounts.test.tsx`, `TotalBalance.test.tsx`, `Insights.test.tsx`, `ProductRecommendations.test.tsx` | |
| UC-03 Download statement | Verified | `GET /api/v1/accounts/{id}/statements` (PDF, CSV), `/statements` | `test_statements.py`, `test_statements_render.py`, `Statements.test.tsx`; E2E `journey-statements.spec.ts` (#472) | |
| UC-04 Transfer money | Verified | `modules/payments/` (review, idempotency, limits, fraud decision, TOTP step-up, escalation, expiry), Send money dialog | `test_transfers.py` (48 tests incl. concurrency), `test_step_up_expiry_and_key_ttl.py`, `SendMoney.test.tsx`; E2E `journey-transfer.spec.ts` (2 journeys) | |
| UC-05 Manage payees | Verified | `/api/v1/payees` add, list, edit, verify, delete; `/payees` | `test_payee_edit.py`, `test_transfers.py::test_payee_name_mismatch_is_not_verified_and_never_reveals_the_real_name`, BOLA matrix, `Payees.test.tsx` | External-bank names cannot be checked in the simulation (`NOT_CHECKED`, stated in the UI) |
| UC-06 Loan recommendation | Verified | Eligibility from income, debt ratio, spending and balance (rules 1-5, ADR-0056, #470); `GET /api/v1/loans/offers`, "Loans for you" on `/loans` and catalogue loans on the carousel; apply, disbursal, tracking (ADR-0024) | `test_loan_offers.py`, `unit/test_loan_eligibility_rules.py`, `ml/tests/test_recommendations_loan_offers.py`, `test_loans.py`, `test_loan_applications.py`, `Loans.test.tsx`; E2E `journey-loans.spec.ts` | |
| UC-07 Investment recommendation | Partial | Risk questionnaire (RTQ-1); `/recommendations` uses income, savings ratio, buffer, balance and the risk band as a suitability rule (#461); wealth catalogue, fixed deposits, comparison; investing API: buying and selling funds, stocks and bonds at a simulated price (#465, in review) | `test_risk_profile.py`, `test_recommendations_risk_band.py`, `ml/tests/test_recommendations_risk_band.py`, `test_wealth.py`, `test_fd_maturity_payout.py`, `test_investing.py` (#465) | Wealth scope in review: investing API (#465), insurance and web (PR pending), fund SIPs (PR pending). Only the index-fund SIP carries a risk level in the engine |
| UC-08 AI assistant | Verified | Nine read-only tools; rules first, Groq or Ollama only to choose a tool; guard, redaction, audit (ADR-0018, ADR-0037); with #474 (in review) FAQ-2 covers every customer feature and product discovery lists the simulated insurance plans | `test_assistant.py`, `test_assistant_adversarial.py`, `test_assistant_groq.py`, `unit/test_assistant_faq.py`, `unit/test_assistant_discovery.py`, `test_unusual_expenses.py`; with #474 `test_every_customer_feature_has_an_faq_entry`; E2E `journey-assistant.spec.ts` | The model path for unclear questions: 0.65-0.75 s with Groq on the demo stack, 12-50 s with Ollama on CPU (the fallback); see NFR table |
| UC-09 Fraud detection | Verified | Baseline rules for every listed trigger, hybrid LightGBM and Isolation Forest with a rules-only fail-safe, structuring monitor, feature snapshot per decision | `test_fraud_hybrid.py`, `test_transfers.py`, `test_fraud_signals.py`, `test_fraud_impossible_travel.py`, `test_fraud_mule.py`, `test_fraud_structuring_dormancy.py`, `test_fraud_structuring_monitor.py`, `ml/tests/` behaviour-regression suite | Location accuracy not evaluated (no labelled location data); circular mule rings are an offline spike, not served |
| UC-10 Fraud alert management | Verified | Alert opened with the decision, customer notified through the outbox, analyst queue and decision with idempotency | `test_transfers.py` (release, reject, label), `test_fraud_clear_scheduled.py`, `test_notifications.py`, `test_outbox_relay.py`, `Fraud.test.tsx`; E2E `journey-fraud-review.spec.ts` (2 journeys) | |
| UC-11 Behaviour analytics | Verified | Analytics consent on the Profile page, off by default and audited (ADR-0057, #463, #477); consent-gated tracker with page visits, clicks and a session heartbeat; sessions and duration, decayed product interests, daily profile job (03:40 UTC); spend and income patterns; RFM segments; staff dashboard with interest and session charts; profile in the RM flyout | `test_behaviour_profile.py`, `unit/test_behaviour_profiling.py`, `test_events.py`, `test_behaviour_analytics.py`, `test_segmentation.py`, `AnalyticsConsentSection.test.tsx`, `tracker.test.ts`, `PageViews.test.tsx`, `BehaviourAnalytics.test.tsx`, `RMInsights.test.tsx` | |
| UC-12 Product recommendations | Verified | `rules-v1` with segment, spending, income, held products, consented age band, risk band (#461) and catalogue loans (#470); gated simulated-uptake ranker only re-orders (ADR-0053) | `test_recommendations.py`, `test_recommendations_held_products.py`, `test_recommendations_risk_band.py`, `test_recommendation_context_cache.py`, `test_loan_offers.py`, `test_demographics.py`, `ml/tests/test_recommendations*.py`, `ProductRecommendations.test.tsx` | The ranker is trained on simulated uptake only (stated in responses and ADR-0053) |
| UC-13 Security monitoring | Verified | `/staff/monitoring` (`GET /fraud/monitoring`), `/staff/fraud-dashboard` (`GET /fraud/stats`, #464) | `test_security_monitoring.py`, `test_fraud_stats.py`, `SecurityMonitoring.test.tsx`, `FraudDashboard.test.tsx`; E2E `shell.spec.ts` | |

## Scope items (section 5)

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Dashboard: account summaries, total balance, recent transactions, spending insights, financial health, recommendations | Verified | UC-02 row | |
| Account services: account details, transaction search, statement viewing and PDF download, settings | Verified | `GET /accounts/{id}`, filtered keyset history (`test_transaction_search.py`), statements (UC-03), profile, devices, phone, password (`test_customers.py`, `Profile.test.tsx`) | |
| Payee management, fund transfers, transaction history | Verified | UC-04, UC-05 rows | Per-account ledger history; no separate list of transfers |
| Bill payments | Verified | `POST /api/v1/bills/pay`, `/bills`; `test_bills.py`, `PayBills.test.tsx`; E2E `journey-bills.spec.ts` (#472) | |
| Scheduled transfers | Verified | `/api/v1/transfers/scheduled`, beat runner (ADR-0023), `/scheduled`; `test_scheduled_transfers_*.py`, `ScheduledTransfers.test.tsx`; E2E `journey-scheduled.spec.ts` | |
| Loans: catalogue, eligibility check, EMI calculator, applications, tracking | Verified | UC-06 row | Penalty interest, prepayment and foreclosure out of scope (ADR-0024) |
| Wealth: fixed deposits | Verified | Booking, closure, automatic maturity payout (ADR-0025, ADR-0029); `test_wealth.py`, `test_fd_maturity_payout.py`, `Wealth.test.tsx` | |
| Wealth: mutual funds; stocks and bonds | Partial | Investing API (#465, in review): funds by amount, stocks and bonds in whole units at a simulated daily price, holdings, sale, portfolio value and gain, through the ledger (ADR-0058); `test_investing.py`, `unit/test_wealth_pricing.py` | Web screens (wealth web PR pending) and fund SIPs (SIP PR pending). On `main` today funds and bonds are listed, not sold, and there are no stocks |
| Wealth: insurance products | Partial | Catalogue with indicative premiums (ADR-0028); `test_insurance.py`, `test_wealth_insurance.py`; the assistant lists the plans (#474, in review) | Quote and application: wealth insurance PR pending |
| Wealth: product comparison | Verified | `/wealth/compare`; `WealthCompare.test.tsx`; E2E `journey-compare.spec.ts` | |
| Assistant: natural language queries, financial guidance, product discovery, spending explanations, FAQ | Verified | UC-08 row; `get_faq_answer` (FAQ-2 for every customer feature with #474), `list_products` (insurance plans with #474), `get_investment_suggestions` | |
| Fraud monitoring dashboard: suspicious activity, risk scoring, alert management, incident review | Verified | Queue and detail (`/staff/fraud`), monitoring (`/staff/monitoring`), charts of `/fraud/stats` with risk bands and alert outcomes (`/staff/fraud-dashboard`, #464) | |
| Behavioural analytics dashboard: segmentation, spending patterns, product affinity, engagement | Verified | `GET /api/v1/insights/behaviour`, `/staff/behaviour`: segment mix, spend by segment, holdings and interest by category (chart), consenting customers, sessions per day (chart) (#463, #477); `test_behaviour_analytics.py`, `BehaviourAnalytics.test.tsx` | Engagement counts consenting customers only (by design, ADR-0057) |
| Compliance and risk dashboard: audit history, system alerts, exception management, regulatory monitoring | Verified | `/staff/audit-log` with chain verification; `/staff/compliance` exceptions: blocked payments, lockouts, chain failures, undelivered notifications and failed standing orders (#466), audited acknowledge and resolve, 24 h indicators (ADR-0027); `test_compliance_exceptions.py`, `ComplianceDashboard.test.tsx` | The 24 h service levels are the bank's own assumption |
| Administration (section 6) | Verified | Staff users, lockouts, allow-listed runtime settings, RM assignment; `test_runtime_config.py`, `test_rm_assignment.py`, `Administration.test.tsx`; E2E `journey-admin.spec.ts` (3 journeys) | No user search: the lockout panel takes a user id |

## Non-functional requirements (section 8)

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Secure authentication | Verified | UC-01 row; route-policy registry (`security/test_route_policy_registry.py`) | |
| Encrypt sensitive data | Partial | Field encryption with rotation (`core/crypto.py`, `test_crypto.py`, `test_rotate_field_key.py`), PII policy (ADR-0042), TLS at nginx and to PostgreSQL (`scripts/verify_tls.sh` in the CI E2E job) | At-rest evidence on the deployment host (#399, #145); certificate issuance on staging (#198) |
| Role-based access control | Verified | `require_roles`, policy functions; `test_rbac_bola.py`, route-policy registry | |
| Audit logs | Verified | Hash-chained append-only `audit_logs`; `test_audit_and_outbox.py`, `AuditLog.test.tsx` | |
| OWASP Top 10 | Verified (locally) | Threat-model mapping, Bandit, CodeQL, pip-audit, npm audit, detect-secrets, nightly Schemathesis, nightly OWASP ZAP baseline against the HTTPS stack ([security audit](../security/security-audit-2026-11.md)) | The ZAP scan is passive (no authenticated active scan) and has not run against a deployed environment (#198) |
| Page load < 2 s | Verified (CI) | `perf.spec.ts` in `perf.yml` with 2 API replicas: pages 110-451 ms ([performance 2026-11-04](../testing/performance-2026-11-04.md)) | Staging page-load budgets (TASK-102, #185) |
| API response < 500 ms | Partial | k6 in `perf.yml`, 50 virtual users, 2 API replicas: every route group p95 under 500 ms (transfers 287 / 347 ms); fraud scoring p95 24.7 ms. Assistant: clear questions are answered by the rules in under 0.1 s; unclear questions that need the model take 0.65-0.75 s with Groq on the demo stack (`engine=llm`, live runs on 5 Nov 2026) and 12-50 s with Ollama on CPU, the fallback when Groq is not configured or fails (ADR-0018, ADR-0037) | Load at 500 users (TASK-078, #101); TASK-079 on the host. **The model path misses 500 ms with both providers; Ollama on CPU misses it by far** |
| Login < 3 s | Verified (CI) | k6 login p95 340-406 ms, browser login 251-277 ms (#419) | Staging measurement (TASK-102, #185) |
| 99% availability in the test environment | Unverified | Blackbox probes, `pbip:availability:ratio_30d`, `AvailabilityBelow99`, the staging probe and report scripts (`infra/deploy/uptime-probe.sh`, `scripts/uptime_report.py`) | Staging availability over a month (TASK-103, #186) needs the staging host (#198) |
| Horizontal scaling, stateless APIs, containers | Verified (CI) | Stateless API, one process per container; **multi-replica exercised in CI**: `perf.yml` runs `API_REPLICAS=2` behind nginx (#419, ADR-0045); staging compose runs 2 replicas | TASK-079 on the host |
| Centralised logging, error tracking, health monitoring, performance metrics | Verified (locally) | structlog to Loki through Alloy, `unhandled_exception` events and `unhandled_errors_total` (`test_errors.py`), health probes, Prometheus metrics and alert rules, Grafana dashboards as code (`test_monitoring_as_code.py`), worker metrics through Redis (ADR-0039), optional OTel collector | **No alert notification channel** (no Alertmanager; owner decision); the monitoring profile is not part of the staging compose |

## Deliverables (section 9)

| Deliverable | Status | Evidence | Gap |
|---|---|---|---|
| Responsive web application | Verified | Customer and staff screens for every use case (`apps/web/src/shell/routes.tsx`); phone-width checks in `journey-compare.spec.ts` and the UI guidelines | Investing, insurance-application and SIP screens wait for the wealth PRs |
| Backend API services | Verified | 106 operations in the OpenAPI contract on `main`; 111 with #465 | |
| Database implementation | Verified | Migrations `0001` to `0030` on `main` (`0031` with #465), up, down, up and `alembic check` in CI; [ER diagram](../architecture/er-diagram.md) generated and drift-checked | |
| AI assistant | Verified | UC-08 row | Model latency (NFR-07) |
| Fraud monitoring dashboard | Verified | Scope row "Fraud monitoring dashboard" | |
| Reporting dashboard | Verified | Behavioural analytics, compliance, security monitoring, fraud dashboard, RM insights, customer insights | |
| Source code repository | Verified | GitHub repository with rulesets | |
| API documentation | Verified | `docs/api/openapi.json` and `apps/web/src/api/schema.d.ts`, both drift-checked in CI; `/docs` on a running API | |
| Automated tests | Verified | Unit, integration, security, ML, Vitest and Playwright suites in CI ([test strategy](../testing/test-strategy-and-report.md)) | |
| CI/CD pipeline | Partial | `ci.yml`, `codeql.yml`, `deploy.yml` (verify, package arm64 and amd64, deploy, smoke, rollback) | The deploy job has never run against a host (#198) |
| Deployment scripts | Unverified | `infra/deploy/deploy.sh`, `docker-compose.prod.yml`, tested by `test_deploy_script.py` | Not run on the staging host (#198) |
| Monitoring configuration | Verified (locally) | Prometheus, Grafana, Loki, Alloy, blackbox and exporters as code | Not deployed with staging (by design of the staging compose) |
| Requirements specification, user stories, use case document | Verified | [requirements-specification.md](requirements-specification.md), [user-stories.md](user-stories.md), [use-cases.md](use-cases.md) | |
| HLD, LLD, ER diagram, API specifications | Verified | [HLD](../architecture/HLD.md), [LLD](../architecture/LLD.md), [ER diagram](../architecture/er-diagram.md) (drift-checked in CI), [diagrams](../diagrams/README.md), OpenAPI | The HLD and LLD are checked by review, not by a CI test |
| Deployment guide, support guide | Verified | [deployment guide](../operations/deployment-guide.md), [support guide](../operations/support-guide.md), [runbook](../operations/runbook.md) (`unit/test_runbook_references.py`), [Oracle guide](../operations/deploy-oracle-cloud-arm.md) | |
| User manual | Partial | [User manual](../user-manual/README.md): customer, staff and compliance guides with screenshots, and sections for the v1.1.0 screens | Screenshots of the v1.1.0 screens are taken on the final stack (list in [use-cases.md](use-cases.md#screenshots-to-capture-on-the-v110-stack)) |

## Engineering practices (section 10)

| Practice | Status | Evidence | Gap |
|---|---|---|---|
| Git, branching strategy, pull requests | Verified | Short-lived `feat/`, `fix/`, `docs/` branches, PR template, squash-only rulesets ([CONTRIBUTING.md](../../CONTRIBUTING.md)) | |
| Peer reviews | Partial | The lead reviews every PR line by line before merge; AI-assisted review; peer review requests per area ([peer-review-requests.md](../testing/peer-review-requests.md)) | **The ruleset requires 0 approvals; the teammates' reviews of the listed PRs are not yet recorded on GitHub** |
| Layered architecture, separation of concerns, design patterns, SOLID | Verified | Router, service, policy or repository, models per module; five import-linter contracts in CI ([module diagrams](../diagrams/module.md)) | |
| Clean code, comments, static analysis, consistent formatting | Verified | ruff (lint and format), `mypy --strict`, ESLint, Prettier, `tsc` strict, Bandit, CodeQL in CI | |
| Unit testing, minimum 70% coverage | Verified | Backend coverage gate 80% in CI; 96.7% in CI run 41689113122 (ledger and auth branch coverage above 90%) | Frontend coverage is reported in CI but has no gate |
| Integration and API testing | Verified | Integration suite against real PostgreSQL, contract test (`test_fuzz_contract.py`), nightly Schemathesis | |
| UI testing of critical journeys | Verified | 11 Playwright journey specs: onboarding with MFA, transfer (2), fraud review (2), scheduled transfer, assistant, BOLA, administration (3), comparison, statements, bills and loans (#472) | |
| Security testing: authentication, authorization, input validation | Verified | `test_auth.py`, `test_rbac_bola.py`, route-policy registry, `test_request_text_validation.py`, adversarial assistant suite, fuzzing | |
| CI: build, test, code quality scan, security scan | Verified | `ci.yml` jobs and `CI Gate`, CodeQL | |
| CI: package, deploy, smoke test | Unverified | `deploy.yml` packages on every tag (v1.0.0 to v1.0.2); deploy and smoke steps gated off | Staging deployment (#198) |

## Success criteria (section 11)

| Criterion | Status | Evidence |
|---|---|---|
| All major customer journeys function correctly | Verified | UC rows above; Playwright journeys in CI |
| Recommendations generated using behavioural data | Verified | UC-12 row (segment, spending, income, held products, risk band; behavioural profile captured with consent) |
| Fraud scenarios generate alerts | Verified | UC-09, UC-10 rows; fraud demo lab (local and test only) |
| AI assistant answers contextual banking questions | Verified | UC-08 row |
| Reporting dashboards provide meaningful insights | Verified | Deliverable "Reporting dashboard" |
| Modular and maintainable architecture | Verified | Practices table |
| Minimum 70% automated test coverage | Verified | Practices table |
| CI/CD pipeline fully functional | Partial | CI verified; deployment to a host not yet run (#198) |
| Secure authentication and authorization | Verified | NFR table |
| Code adheres to coding standards | Verified | Practices table |
| Documentation complete and professionally presented | Partial | Documentation index ([docs/README.md](../README.md)); v1.1.0 screens and screenshots pending |
| Repeatable automated deployment | Unverified | Tagged release, Deploy workflow and `deploy.sh`; not yet run against the staging host (#198) |

## Gaps that need the staging server

Everything below is implemented and tested locally or in CI and waits only for a deployed host.

| Gap | Issue or task |
|---|---|
| First staging deployment through the Deploy workflow, with smoke test and rollback proof | #198 |
| Availability measured over a month on staging | TASK-103, #186 |
| Load at 500 users | TASK-078, #101 |
| Horizontal scaling run on the host | TASK-079 |
| Page-load and login budgets measured against staging | TASK-102, #185 |
| Encryption-at-rest evidence on the deployment host | #399, #145 |
| TASK-089 (demo on the deployed stack) | depends on #399 |

## Scope decisions

1. **New geographic location (UC-09)**: DB-IP Lite, country and city level (ADR-0014, ADR-0034).
2. **Demographics (UC-12)**: an optional, consented date of birth used only as a derived age band (ADR-0015).
3. **Behavioural data (UC-11)**: recorded only with the customer's analytics consent (ADR-0057).
4. **Simulated markets (section 5 wealth)**: prices are simulated and labelled so (ADR-0058); no market data.
