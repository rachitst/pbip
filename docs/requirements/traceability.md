# Requirements traceability: use case document v0.2 vs. implementation

Source of truth: *Personal Banking Intelligence Platform (PBIP) Use Case Document v0.2* (sections 5,
7, 8, 9, 10, 11). This checklist records what the code on `main` actually does and which tests
prove it. It is updated whenever a feature PR merges.

Status values:

* **Verified**: implemented, and an automated test that runs in CI proves the behaviour.
* **Partial**: some of the requirement is implemented and verified; the rest is missing (stated).
* **Unverified**: implemented or configured, but never exercised by a test or a real environment.
* **Missing**: not implemented (an empty module or no code at all).

Last reviewed against `main` on 2026-10-30.

## Functional use cases (section 7)

| Use case | Status | Code | Tests (CI) | Gap |
|---|---|---|---|---|
| UC-01 Customer login (credentials, MFA, failed-login events) | Verified | `modules/auth/` (Argon2id, JWT + rotating refresh cookie, TOTP MFA, lockout, security events), `pages/Login.tsx` | `test_auth.py`, `test_admin_lockout.py`, `test_passwords.py`, `test_ratelimit.py`, E2E `sprint1.spec.ts` | MFA is optional for customers (mandatory for staff) |
| UC-02 Banking dashboard (balance, recent transactions, spending insights, recommendations) | Partial | Accounts and transactions APIs + `pages/Accounts.tsx`, `pages/AccountTransactions.tsx`; spending and health-score APIs (`modules/analytics/`) | `test_accounts.py`, `test_transaction_pagination.py`, `test_analytics.py` | No dashboard page; spending insights and health score have no UI; no product recommendations |
| UC-03 Download account statement (PDF) | Missing | `modules/statements/` is empty | none | Whole use case |
| UC-04 Transfer money (balance, fraud checks, limits) | Partial | `modules/payments/`, `modules/ledger/`, fraud scoring, idempotency, TOTP step-up | `test_transfers.py`, `test_ledger.py`, `test_ledger_guards.py`, `test_rbac_bola.py` | API only; no transfer UI; no bill payments or scheduled transfers |
| UC-05 Manage payees (add, edit, delete, verify) | Partial | `modules/payees/` (add, list, get, delete, verify) | `test_rbac_bola.py`, payee flows in `test_transfers.py` | No edit endpoint; no UI |
| UC-06 Loan recommendation | Missing | `modules/loans/` is empty; the health score (`analytics/health.py`) is a possible input | none | Whole use case (catalogue, eligibility, EMI calculator, applications, tracking) |
| UC-07 Investment recommendation | Missing | `modules/wealth/`, `modules/recommendations/` are empty | none | Whole use case |
| UC-08 AI assistant interaction | Missing | `modules/assistant/` is empty | none | Whole use case (AGENTS.md section 9 defines the required design) |
| UC-09 Fraud detection (large amount, login failures, new location, rapid transactions, unusual behaviour) | Partial | `modules/fraud/baseline.py` rules: LARGE_AMOUNT, VELOCITY_BURST, AMOUNT_SPIKE, NEW_PAYEE, NEW_PAYEE_LARGE_AMOUNT; risk score + reasons persisted | `test_transfers.py` (fraud paths), `test_rbac_bola.py` | No rule for multiple login failures or new geographic location; no ML model in the request path |
| UC-10 Fraud alert management (alert, customer notified, analyst review, resolution) | Partial | `modules/fraud/router.py` (list, detail, decision with MFA), outbox notifications | `test_transfers.py`, `test_audit_and_outbox.py`, `test_notifications.py` | No analyst UI (fraud monitoring dashboard) |
| UC-11 Customer behaviour analytics (page visits, clickstream, session duration, product interests, spend and income patterns) | Partial | Spend patterns (`/analytics/spending`, `/trends`), RFM + lifestyle segmentation (`segmentation_job.py`) | `test_analytics.py`, `test_segmentation.py`, `ml/tests/test_segmentation.py` | No clickstream, page-visit or session capture; no product-interest data |
| UC-12 Product recommendation engine | Missing | none | none | Whole use case; segmentation exists as an input |
| UC-13 Security monitoring dashboard | Partial | Compliance audit-log API with hash-chain verification; Grafana/Prometheus/Loki config in `infra/` | `test_audit_and_outbox.py` | No security dashboard UI; dashboards in `infra/grafana` not exercised in CI |

## Scope items without a use case (section 5)

| Item | Status | Note |
|---|---|---|
| Financial health indicators | Verified (API) | `GET /analytics/health-score`; no UI |
| Customer segmentation (behavioural dashboard) | Partial | Offline K-Means job and `GET /analytics/segment`; job not scheduled; no staff dashboard |
| Account settings, devices, password change | Verified | `pages/Profile.tsx`, `test_customers.py`, E2E `sprint1.spec.ts` |
| Bill payments, scheduled transfers | Missing | |
| Loans, wealth (FD, mutual funds, insurance, stocks), product comparison | Missing | |
| Compliance and risk dashboard (exceptions, regulatory monitoring) | Partial | Audit log API only |

## Non-functional requirements (section 8)

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Secure authentication | Verified | auth tests, route-policy registry test | |
| Encrypt sensitive data | Verified | `core/crypto.py` FieldCipher (account numbers, TOTP secrets), `test_crypto.py`, `test_rotate_field_key.py` | TLS is not configured in the repo (runbook section 1.4) |
| Role-based access control | Verified | `require_roles`, `test_rbac_bola.py`, `test_route_policy_registry.py` | |
| Audit logs | Verified | hash-chained `audit_logs`, `test_audit_and_outbox.py` | |
| OWASP Top 10 | Partial | `docs/security/threat-model.md` mapping, Bandit, CodeQL, nightly fuzzing (0 5xx in run 40914561750) | No DAST against a deployed environment |
| Page load < 2 s, API < 500 ms, login < 3 s | Unverified | `infra/k6/baseline.js` exists | Not run in CI; no recorded measurement |
| 99% availability (test environment) | Unverified | | No test environment has been deployed |
| Horizontal scaling, stateless APIs, containers | Partial | Docker images, compose files; JWT access tokens | Multi-replica deployment never exercised |
| Centralised logging, error tracking, health, metrics | Unverified | `infra/{loki,alloy,prometheus,grafana}`, `/api/health/*` | Configuration only; no deployed stack observed |

## Engineering deliverables and practices (sections 9 to 11)

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Responsive web application | Partial | React app: login, accounts, transactions, profile | Most journeys have no UI |
| Backend API services, database | Verified | 38 operations in `docs/api/openapi.json`; Alembic migrations 0001 to 0005 (0005 = ledger account/time index) | |
| AI assistant, fraud dashboard, reporting dashboard | Missing | | |
| API documentation | Verified | OpenAPI contract, drift-checked in CI | |
| Automated tests, >= 70% coverage | Verified (backend) | CI gate 80%, latest run 96% | Frontend coverage threshold not verified here |
| UI testing of critical journeys | Partial | Playwright: login, account opening, device, password change, isolation | Transfers, payees, statements have no E2E |
| Security testing (authn, authz, input validation) | Verified | security tests, BOLA matrix, fuzzing | |
| CI: build, test, quality, security scan | Verified | `.github/workflows/ci.yml`, CodeQL | |
| CI: package, deploy, smoke test | Unverified | `.github/workflows/deploy.yml`, `infra/deploy/deploy.sh` | Gated off (`DEPLOY_ENABLED` unset); never run against a host |
| Monitoring configuration | Unverified | `infra/` | Not deployed |
| Peer reviews | Partial | PR template, rulesets | The ruleset requires 0 approvals; several PRs were merged on maintainer approval only |
| Documentation: HLD/LLD/ER/API | Partial | `docs/PROJECT_BLUEPRINT.md`, ADRs, OpenAPI | No separate HLD/LLD documents; ER diagram coverage not verified here |
| Documentation: deployment guide, support guide | Verified | `docs/SETUP_GUIDE.md`, `docs/operations/runbook.md` | |
| Documentation: user manual, requirements spec, user stories | Partial | backlog and execution plan carry user-facing outcomes | No user manual |

## Most important gaps (by project impact)

1. **AI assistant (UC-08)**: a success criterion and a functional deliverable; nothing implemented.
2. **Recommendations (UC-06, UC-07, UC-12)**: success criterion "product recommendations are
   generated using behavioural data" is unmet; loans and wealth modules are empty.
3. **Dashboards (UC-02, UC-10, UC-13, behavioural analytics)**: success criterion "reporting
   dashboards provide meaningful insights" is unmet; the APIs exist but there is no UI.
4. **Statements PDF (UC-03)** and **bill payments / scheduled transfers**.
5. **Deployment and NFR evidence**: the deploy pipeline, performance targets and availability have
   never been exercised against a real environment.
