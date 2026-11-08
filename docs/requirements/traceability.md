# Requirements traceability: use case document v0.2 vs. code and tests

Source of truth: *Personal Banking Intelligence Platform (PBIP) Use Case Document v0.2*, sections 5 (scope), 7
(UC-01 to UC-13), 8 (non-functional), 9 (deliverables), 10 (engineering practices) and 11 (success criteria). This
file records, for every requirement, what the code does, which automated tests prove it, and what is missing.

**Checked against** `main` at `60b1af4` (5 Nov 2026), the v1.1.0 code: it includes #461, #462, #463, #464, #465, #466,
#467, #468, #469, #470, #472, #473, #474 (assistant FAQ for every feature and insurance discovery), #477, #483
(insurance quote and application, web Invest, Insurance and Holdings pages), #484 (fund SIPs with an hourly job) and
#487 (SIP web screens). **Updated 8 Nov 2026** with the staging host (deploy guide), #494, #496, #497, #498 and #499.

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
| UC-07 Investment recommendation | Verified | Risk questionnaire (RTQ-1); `/recommendations` uses income, savings ratio, buffer, balance and the risk band as a suitability rule (#461); fixed deposits; buying and selling funds, stocks and bonds at a simulated price through the ledger (#465), with the `/wealth/invest` page and holdings (#483); fund SIPs (#484, web #487); suitability on `/wealth/compare` (#483) | `test_risk_profile.py`, `test_recommendations_risk_band.py`, `ml/tests/test_recommendations_risk_band.py`, `test_wealth.py`, `test_fd_maturity_payout.py`, `test_investing.py`, `test_fund_sips.py`, `test_wealth_suitability.py`, `Invest.test.tsx`, `Wealth.test.tsx`, `WealthCompare.test.tsx` | Prices are simulated by design (ADR-0058) |
| UC-08 AI assistant | Verified | Nine read-only tools; rules first, Groq or Ollama only to choose a tool; guard, redaction, audit (ADR-0018, ADR-0037); with #474 FAQ-2 covers every customer feature and product discovery lists the simulated insurance plans | `test_assistant.py`, `test_assistant_adversarial.py`, `test_assistant_groq.py`, `unit/test_assistant_faq.py`, `unit/test_assistant_discovery.py`, `test_unusual_expenses.py`; `test_every_customer_feature_has_an_faq_entry`; E2E `journey-assistant.spec.ts` | The model path for unclear questions: 0.65-0.75 s with Groq on the demo stack, 12-50 s with Ollama on CPU (the fallback); see NFR table |
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
| Wealth: mutual funds; stocks and bonds | Verified | Funds by amount, stocks and bonds in whole units at a simulated daily price, holdings, sale, portfolio value and gain, through the ledger (ADR-0058, #465); `/wealth/invest` and holdings with sell (#483); monthly fund SIPs bought exactly once per day by the hourly `run-fund-sips` job (#484), started on `/wealth/invest` and listed with "Cancel SIP" on `/wealth` (#487); `test_investing.py` (incl. concurrency), `unit/test_wealth_pricing.py`, `test_fund_sips.py`, `Invest.test.tsx`, `Wealth.test.tsx` | Simulated prices, no market data (by design) |
| Wealth: insurance products | Verified | Catalogue with indicative premiums (ADR-0028); simulated quote and application with documented age-band and term factors and deterministic status rules, no premium debited, no policy issued (ADR-0058, #483); `/wealth/insurance`; the assistant lists the plans (#474); `test_insurance.py`, `test_insurance_applications.py`, `unit/test_wealth_insurance.py`, `InsuranceQuote.test.tsx` | Simulated by design: a SUBMITTED (referred) application has no later step |
| Wealth: product comparison | Verified | `/wealth/compare` with "Suits your risk band" from `GET /wealth/suitability` (#483); `WealthCompare.test.tsx`, `test_wealth_suitability.py`; E2E `journey-compare.spec.ts` | |
| Assistant: natural language queries, financial guidance, product discovery, spending explanations, FAQ | Verified | UC-08 row; `get_faq_answer` (FAQ-2 for every customer feature with #474), `list_products` (insurance plans with #474), `get_investment_suggestions` | |
| Fraud monitoring dashboard: suspicious activity, risk scoring, alert management, incident review | Verified | Queue and detail (`/staff/fraud`), monitoring (`/staff/monitoring`), charts of `/fraud/stats` with risk bands and alert outcomes (`/staff/fraud-dashboard`, #464) | |
| Behavioural analytics dashboard: segmentation, spending patterns, product affinity, engagement | Verified | `GET /api/v1/insights/behaviour`, `/staff/behaviour`: segment mix, spend by segment, holdings and interest by category (chart), consenting customers, sessions per day (chart) (#463, #477); `test_behaviour_analytics.py`, `BehaviourAnalytics.test.tsx` | Engagement counts consenting customers only (by design, ADR-0057) |
| Compliance and risk dashboard: audit history, system alerts, exception management, regulatory monitoring | Verified | `/staff/audit-log` with chain verification; `/staff/compliance` exceptions: blocked payments, lockouts, chain failures, undelivered notifications and failed standing orders (#466), audited acknowledge and resolve, 24 h indicators (ADR-0027); `test_compliance_exceptions.py`, `ComplianceDashboard.test.tsx` | The 24 h service levels are the bank's own assumption |
| Administration (section 6) | Verified | Staff users, lockouts, allow-listed runtime settings, RM assignment; `test_runtime_config.py`, `test_rm_assignment.py`, `Administration.test.tsx`; E2E `journey-admin.spec.ts` (3 journeys) | No user search: the lockout panel takes a user id |

## Non-functional requirements (section 8)

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Secure authentication | Verified | UC-01 row; route-policy registry (`security/test_route_policy_registry.py`) | |
| Encrypt sensitive data | Verified | Field encryption with rotation (`core/crypto.py`, `test_crypto.py`, `test_rotate_field_key.py`), PII policy (ADR-0042), TLS at nginx and to PostgreSQL (`scripts/verify_tls.sh` in the CI E2E job). **On the staging host (8 Nov 2026):** encrypted (`gpg`) daily backups with the off-host copy in OCI Object Storage (`infra/deploy/backup-offsite.sh`, `scripts/offsite_backup.py`, #496), and a restore drill of the copy downloaded from the bucket: **PASS** (revision 0031, ledger net 0, audit chain valid); the backup passphrase is a separate file from the `.env` that holds `FIELD_ENCRYPTION_KEY` (both still to be copied into the owner's password manager). Docker's data is on the instance's boot volume, encrypted at rest with an Oracle-managed key (confirmed in the OCI console, #399). Let's Encrypt TLS with HSTS on `https://pbip.duckdns.org` (TLSv1.3, `http://` redirects); ADR-0042 is no longer provisional | Key custody: the backup passphrase and the `.env` secrets still to be copied into the owner's password manager (operator task) |
| Role-based access control | Verified | `require_roles`, policy functions; `test_rbac_bola.py`, route-policy registry | |
| Audit logs | Verified | Hash-chained append-only `audit_logs`; `test_audit_and_outbox.py`, `AuditLog.test.tsx` | |
| OWASP Top 10 | Verified (locally) | Threat-model mapping, Bandit, CodeQL, pip-audit, npm audit, detect-secrets, nightly Schemathesis, nightly OWASP ZAP baseline against the HTTPS stack ([security audit](../security/security-audit-2026-11.md)) | The ZAP scan is passive (no authenticated active scan) and has not run against a deployed environment (#198) |
| Page load < 2 s | Verified (CI and staging) | `perf.spec.ts` in `perf.yml` with 2 API replicas: pages 110-451 ms ([performance 2026-11-04](../testing/performance-2026-11-04.md)); against `https://pbip.duckdns.org` from a laptop, 3 runs: worst page 1,357 ms (`/` signed out, empty cache), signed-in reloads 464-1,015 ms ([performance 2026-11-08](../testing/performance-2026-11-08-oci.md), TASK-102) | |
| API response < 500 ms | Partial | k6 in `perf.yml`, 50 virtual users, 2 API replicas: every route group p95 under 500 ms (transfers 287 / 347 ms); fraud scoring p95 24.7 ms. Assistant, first token through nginx on the demo stack after the ADR-0037 addendum (8 Nov 2026, [performance 2026-11-08](../testing/performance-2026-11-08-oci.md)): clear questions p95 66 ms; 13 of 30 open questions answered by the rules at p95 72 ms; the 17 routed by Groq p50 582 ms, p95 754 ms, max 850 ms (before: p50 746 ms, p95 3,094 ms, max 8,093 ms); after the budget change (#502, 10 a minute, a used budget goes straight to the rules) all 30 open questions p95 724 ms, max 757 ms, the 20 routed by Groq p50 626 ms. Ollama on CPU (12-50 s) is the fallback when Groq is not configured or fails (ADR-0018, ADR-0037). On the 2-OCPU staging host the budgets are missed from 50 VUs upward, with the load generator on the host and, in the second pass, off it (about 75 requests/s at 86-91% CPU; capacity, [performance 2026-11-08](../testing/performance-2026-11-08-oci.md)) | **The Groq model path misses 500 ms (p50 582 ms); Ollama on CPU misses it by far.** The 2-OCPU host cannot carry the k6 suite within budget |
| Login < 3 s | Verified (CI and staging) | k6 login p95 340-406 ms, browser login 251-277 ms (#419); browser login on staging 675-868 ms over 3 runs (TASK-102) | Under load on the 2-OCPU host login p95 exceeds 3 s from 100 VUs (TASK-078) |
| 99% availability in the test environment | Partial | Blackbox probes, `pbip:availability:ratio_30d`, `AvailabilityBelow99`, and on the staging host the 30-second probe and weekly report (`infra/deploy/uptime-probe.sh`, `scripts/uptime_report.py`) running since 2026-11-07T21:56:03Z (on wall-clock ticks since 2026-11-08T00:02:30Z, the start of the measured week); the monitoring stack for the production project (ADR-0059, #498) | A full week of probes ends 2026-11-15T00:02:30Z (TASK-103, #186); the month after that |
| Horizontal scaling, stateless APIs, containers | Verified (CI) | Stateless API, one process per container; **multi-replica exercised in CI**: `perf.yml` runs `API_REPLICAS=2` behind nginx (#419, ADR-0045); staging runs 2 replicas; on the staging host 2 replicas with admission control (ADR-0060) carry 500 VUs with 0 errors | Latency budgets on the 2-OCPU host (TASK-078) |
| Centralised logging, error tracking, health monitoring, performance metrics | Verified (locally and on staging) | structlog to Loki through Alloy, `unhandled_exception` events and `unhandled_errors_total` (`test_errors.py`), health probes, Prometheus metrics and alert rules, Grafana dashboards as code (`test_monitoring_as_code.py`), worker metrics through Redis (ADR-0039), optional OTel collector. On staging the monitoring stack runs against `pbip-prod` (ADR-0059): all 10 Prometheus targets up, worker and beat logs in Loki (the 02:00 and Sunday 03:00 UTC analytics runs of 8 Nov were read from it) | **No alert notification channel** (no Alertmanager; owner decision) |

## Deliverables (section 9)

| Deliverable | Status | Evidence | Gap |
|---|---|---|---|
| Responsive web application | Verified | Customer and staff screens for every use case (`apps/web/src/shell/routes.tsx`), including `/wealth/invest`, `/wealth/insurance` (#483) and the SIP screens (#487); phone-width checks in `journey-compare.spec.ts` and the UI guidelines | |
| Backend API services | Verified | 106 operations in the OpenAPI contract on `main` before the wealth PRs; #465, #483 and #484 add the investing, insurance, suitability and SIP routes | |
| Database implementation | Verified | Migrations `0001` to `0031` (`0031` from #465 creates the investing, SIP and insurance-application tables), up, down, up and `alembic check` in CI; [ER diagram](../architecture/er-diagram.md) generated and drift-checked | |
| AI assistant | Verified | UC-08 row | Model latency (NFR-07) |
| Fraud monitoring dashboard | Verified | Scope row "Fraud monitoring dashboard" | |
| Reporting dashboard | Verified | Behavioural analytics, compliance, security monitoring, fraud dashboard, RM insights, customer insights | |
| Source code repository | Verified | GitHub repository with rulesets | |
| API documentation | Verified | `docs/api/openapi.json` and `apps/web/src/api/schema.d.ts`, both drift-checked in CI; `/docs` on a running API | |
| Automated tests | Verified | Unit, integration, security, ML, Vitest and Playwright suites in CI ([test strategy](../testing/test-strategy-and-report.md)) | |
| CI/CD pipeline | Partial | `ci.yml`, `codeql.yml`, `deploy.yml` (verify, package arm64 and amd64, deploy, smoke, rollback); the `staging` environment, its four secrets and `STAGING_URL` are set (8 Nov 2026); tag pushes publish the arm64 images that the staging host pulls | The deploy job's smoke test needs `https://pbip.duckdns.org` reachable: TCP 80/443 to the instance (#198) |
| Deployment scripts | Partial | `infra/deploy/deploy.sh`, `docker-compose.prod.yml`, tested by `test_deploy_script.py`; **run on the staging host on 8 Nov 2026**: `deploy.sh deploy ghcr.io/rachitst/pbip v1.1.0` migrated an empty database to 0031 and recorded the release once every container was healthy, after #494 fixed the worker and beat health checks it found | Run by the Deploy workflow (#198) |
| Monitoring configuration | Verified (locally) | Prometheus, Grafana, Loki, Alloy, blackbox and exporters as code | Not deployed with staging (by design of the staging compose) |
| Requirements specification, user stories, use case document | Verified | [requirements-specification.md](requirements-specification.md), [user-stories.md](user-stories.md), [use-cases.md](use-cases.md) | |
| HLD, LLD, ER diagram, API specifications | Verified | [HLD](../architecture/HLD.md), [LLD](../architecture/LLD.md), [ER diagram](../architecture/er-diagram.md) (drift-checked in CI), [diagrams](../diagrams/README.md), OpenAPI | The HLD and LLD are checked by review, not by a CI test |
| Deployment guide, support guide | Verified | [deployment guide](../operations/deployment-guide.md), [support guide](../operations/support-guide.md), [runbook](../operations/runbook.md) (`unit/test_runbook_references.py`), [Oracle guide](../operations/deploy-oracle-cloud-arm.md) | |
| User manual | Verified | [User manual](../user-manual/README.md): customer, staff and compliance guides, including the v1.1.0 screens with screenshots captured on the v1.1.0 stack ([list](use-cases.md#screenshots-of-the-v110-screens)) | |

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
| CI: package, deploy, smoke test | Partial | `deploy.yml` packages amd64 and arm64 images on every tag (v1.0.0 to v1.1.0); the staging host pulls them; the `staging` environment and its secrets are set | The deploy and smoke steps against the host wait for TCP 80/443 to reach it (#198) |

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
| Documentation complete and professionally presented | Verified | Documentation index ([docs/README.md](../README.md)), user manual with the v1.1.0 screens, link check in CI |
| Repeatable automated deployment | Partial | Tagged release, Deploy workflow and `deploy.sh`; `deploy.sh` ran on the staging host on 8 Nov 2026 (v1.1.0, migrations to 0031, health-gated); the workflow run waits for TCP 80/443 to the instance (#198) |

## Gaps that needed the staging server

The staging host (Oracle Cloud Always Free Arm VM, `pbip.duckdns.org`, deploy guide) exists since 8 November 2026.
Status of each item on that date (updated 8 Nov 04:40 UTC, after TCP 80/443 opened):

| Gap | Issue or task | Status on 8 Nov 2026 |
|---|---|---|
| First staging deployment through the Deploy workflow, with smoke test and rollback proof | #198 | Done: the Deploy workflow deployed v1.1.1 with its smoke test passing over HTTPS (run 42442978136) and a dry run passed (42443924122); v1.1.2 deployed on its tag push (run 42451892070, smoke test green); rollback proved on the host (v1.1.0 restored in 32 s). Let's Encrypt certificate since 2026-11-08T03:26Z, renewal dry run passed. Beat schedules fired at 02:00 UTC and Sunday 03:00 UTC with one beat (worker logs in Loki). GeoIP country DB mounted and read by the running API. Cutover measured at 12-15 s, not zero-downtime (#505). Open: the `NEW_COUNTRY` check with a real public client IP (staging cannot fund an account; a local or tunnelled stack sees only private addresses) |
| Availability measured over a month on staging | TASK-103, #186 | Probe running every 30 s since 2026-11-07T21:56Z, on wall-clock ticks since 2026-11-08T00:02:30Z and against the Let's Encrypt certificate since 03:30Z; one week ends 2026-11-15T00:02:30Z |
| Load at 500 users | TASK-078, #101 | 500 VUs run with 0 errors and every money invariant held (ADR-0060), with the load generator on the host and, since 8 Nov 04:10Z, off it. **The 500 ms p95 at 500 users is not met and is hardware-bound on the 2-OCPU Always Free shape** (about 75 requests/s at 86-91% CPU; [performance 2026-11-08](../testing/performance-2026-11-08-oci.md)); the budget is met in CI on 4 vCPUs at 50 VUs only. Not closed: the definition of done asks for the budget at 500 VUs |
| Horizontal scaling run on the host | TASK-079 | Done: 2 replicas on the host; the pool starvation found there is fixed (ADR-0060, #499); no statement needs an index (the per-user advisory lock waits grow with CPU starvation, not contention) |
| Page-load and login budgets measured against staging | TASK-102, #185 | Done: every budget met on `https://pbip.duckdns.org` in 3 runs (worst page 1,357 ms, worst login 868 ms) |
| Encryption-at-rest evidence on the deployment host | #399, #145 | Done: boot volume encrypted with an Oracle-managed key (OCI console), encrypted daily backups copied off-host, PASS restore drill of the downloaded copy, TLS with HSTS from outside; ADR-0042 is no longer provisional |
| TASK-089 (demo on the deployed stack) | depends on #399 | Customer pages on staging: 0 console errors, 0 page errors, 0 server errors across 12 pages. Staff pages and money movement stay on the local stack: staging has no staff bootstrap or funding by design (deploy guide section 14) |
| Trained models on staging | ADR-0061, #503 | Done: since the v1.1.2 deploy both API replicas serve `hybrid-lgbm-iforest-3` and `sim-propensity-v2` (the demo stack's artefacts, read-only mounts); at rest CPU p95 8.3%, memory 18.9%, still under Oracle's 20% idle line |

## Scope decisions

1. **New geographic location (UC-09)**: DB-IP Lite, country and city level (ADR-0014, ADR-0034).
2. **Demographics (UC-12)**: an optional, consented date of birth used only as a derived age band (ADR-0015).
3. **Behavioural data (UC-11)**: recorded only with the customer's analytics consent (ADR-0057).
4. **Simulated markets (section 5 wealth)**: prices are simulated and labelled so (ADR-0058); no market data.
