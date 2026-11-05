# PBIP requirements specification

| | |
|---|---|
| Task | TASK-104 (#187) |
| Source | *PBIP Use Case Document v0.2*: sections 5 (scope), 6 (actors), 7 (UC-01 to UC-13), 8 (non-functional), 9 to 11 (deliverables, practices, success criteria) |
| Status checked against | `main` at `775ec1a` (5 Nov 2026), plus the PRs merging on 5 Nov: #474 (batch L #481) and the wealth scope of ADR-0058: investing in funds, stocks and bonds through the ledger (#465), the insurance quote and application with the web Invest, Insurance and Holdings pages (#483), and fund SIPs with an hourly job (#484) and their web screens (#487). A row whose evidence is only in one of those PRs names it |
| Companion documents | [Use cases as built](use-cases.md) (flows, screens and tests per use case), [user stories](user-stories.md) (acceptance criteria and tests per story), [traceability](traceability.md) (requirement to code, tests and gaps), [HLD](../architecture/HLD.md) |

Every requirement below is derived from a sentence in the use-case document. Its **Source** column says which one.
The document's wording is kept; where it was ambiguous, the interpretation chosen is stated in the requirement.
Nothing here adds scope the document does not ask for.

**Status.** Each requirement has one of three statuses:

* **Implemented**: the behaviour is on `main` and an automated test in CI proves it.
* **Partial**: part of the requirement is on `main`. The *Gap* column says what is missing.
* **Planned**: none of it is on `main` yet.

For Partial and Planned, the *Tasks* column names the backlog task that owns the missing work. **"No task"** means the
gap is real and nobody owns it yet. These gaps are listed again in section 5.

**Priority.** Priorities come from the backlog (`must`, `should`, `could`, from `docs/backlog/backlog.toml`). Where
the work is spread over several tasks, the requirement takes the highest of their priorities.

## 1. Actors

| ID | Actor (use-case document section 6) | Role in the system |
|---|---|---|
| A1 | Retail customer | `CUSTOMER` role with a customer profile |
| A2 | Relationship manager | `RELATIONSHIP_MANAGER`. Reviews insights and logs outreach only for the customers an administrator assigned to them (ADR-0032) |
| A3 | Fraud analyst | `FRAUD_ANALYST` |
| A4 | Compliance officer | `COMPLIANCE_OFFICER` |
| A5 | Banking administrator | `ADMIN`. Manages staff users and lockouts, changes the allow-listed runtime settings (ADR-0031), and assigns, reassigns and unassigns customers to RMs; every change is audited (ADR-0032) |
| A6 | AI assistant | Not a role. It acts only on behalf of the authenticated customer, through read-only tools |

Every staff role (A2 to A5) must have completed TOTP in the current session to call staff endpoints.

## 2. Functional requirements

### 2.1 Access and identity (UC-01)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-01 | The system shall authenticate a customer with an e-mail and password. | UC-01 flow 1-2 | Implemented | TASK-006 | |
| FR-02 | The system shall support TOTP multi-factor authentication with single-use recovery codes. A customer with MFA enabled must enter a code before access is granted. | UC-01 flow 3 | Implemented | TASK-006; #201 (UI) | MFA is optional for customers (mandatory for staff); enrolment screens on Profile & security and staff Sign-in security (`MfaEnrolment.tsx`, #201) |
| FR-03 | The system shall record a security event for every failed login and MFA failure, and lock the account progressively after repeated failures. | UC-01 alternative flow | Implemented | TASK-006, TASK-007 | |
| FR-04 | The system shall issue short-lived access tokens and rotating refresh tokens, revoke a token family on reuse, and revoke all sessions on password change. | UC-01, section 8 "secure authentication" | Implemented | TASK-006 | |
| FR-05 | An administrator shall be able to create staff users with explicit roles and inspect and clear a user's lockout. Each action is audited. | Section 6 (administrator) | Implemented | TASK-007; TASK-108 (#282, merged #346) | Web screens on `/staff/admin`. The runtime-configurable settings are exactly the ADR-0031 allow-list (daily transfer limit, per-transfer limit, standing-order cap, each bounded); secrets and security settings are deployment configuration only. E2E journey `e2e/journey-admin.spec.ts` (#359) |
| FR-06 | A retail customer shall be able to self-register (disabled in production by configuration). | UC-01 precondition "valid customer account exists" | Implemented | #201 (UI) | Web registration page (`Register.tsx`, #201) |

### 2.2 Dashboard and account services (UC-02, UC-03, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-07 | After login, the customer shall see every account with its available, ledger and held balance. | UC-02, section 5 "account summaries" | Implemented | TASK-019 | |
| FR-08 | The dashboard shall show the total balance across the customer's accounts. | Section 5 "total balance overview" | Implemented | TASK-109 (#283, merged #330) | `GET /api/v1/accounts/summary`, per currency, savings and current accounts only (see US-04) |
| FR-09 | The customer shall see recent transactions for an account, paginated. | UC-02, section 5 | Implemented | TASK-008, TASK-019 | |
| FR-10 | The customer shall see spending insights: category breakdown, month-over-month trends, daily burn rate. | UC-02, section 5 "spending insights" | Implemented | TASK-040, TASK-049 | |
| FR-11 | The customer shall see financial health indicators: a 0-100 health score with components and tips, and a balance forecast labelled as a prediction. | Section 5 "financial health indicators" | Implemented | TASK-041, TASK-042, TASK-049, TASK-050 | Forecast chart on `/insights` (`BalanceForecastSection.tsx`, TASK-050) |
| FR-12 | The dashboard shall show personalised product recommendations. | UC-02, section 5 | Implemented | TASK-054, TASK-062; #461, #470 | Carousel on `/accounts` with reasons; `rules-v1` with the gated simulated-uptake ranker (ADR-0053); risk band (#461) and catalogue loans (#470) |
| FR-13 | The customer shall be able to open an account and view its details. | Section 5 "view account details" | Implemented | TASK-005 | |
| FR-14 | The customer shall be able to search and filter transactions by date and amount. | Section 5 "transaction search" | Implemented | TASK-017 | Filters on the account history (`AccountTransactions.tsx`) |
| FR-15 | The customer shall be able to view a statement for a chosen account and period and download it as PDF. | UC-03, section 5 | Implemented | TASK-016, TASK-074 | PDF or CSV from `/statements` (`Statements.tsx`, `modules/statements`) |
| FR-16 | The customer shall be able to manage account settings: view the profile, list and register devices, change the password. | Section 5 "account settings management" | Implemented | TASK-004, TASK-006 | |
| FR-17 | The customer shall receive in-app notifications (transfer posted, under review, declined; bill payment declined) with an unread badge, and can mark them read. | UC-10 "customer is notified" | Implemented | TASK-018, TASK-022 | |

### 2.3 Payments and transfers (UC-04, UC-05, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-18 | The customer shall transfer money from a chosen source account to a verified payee. The steps are: choose source and payee, enter an amount, review, confirm. | UC-04 flow | Implemented | TASK-037 | |
| FR-19 | A transfer shall be refused when the available balance is insufficient, and leave no partial state. | UC-04 validation "sufficient balance" | Implemented | baseline ledger and payments (before the backlog), TASK-005 | |
| FR-20 | A transfer shall be refused above the per-transfer limit (₹2,00,000) or above the daily limit (₹5,00,000 per UTC day, shared with bill payments). | UC-04 validation "transfer limits" | Implemented | baseline payments (before the backlog), TASK-014 | |
| FR-21 | Every transfer shall be fraud-scored before posting. ALLOW posts it; STEP_UP requires a TOTP code (3 wrong codes, or no MFA enrolled, hand it to an analyst); REVIEW holds the funds for an analyst; BLOCK declines it. The customer never sees the reasons. | UC-04 validation "fraud checks" | Implemented | TASK-025, TASK-033 | |
| FR-22 | Money-moving requests shall be idempotent: a retry with the same key returns the original result and never moves money twice. | UC-04 outcome "successfully submitted" (interpreted for safe retries) | Implemented | baseline payments (before the backlog), TASK-014; #314 | The step-up confirmation and the analyst decision also require the key (#364); keys expire after their TTL and are purged daily (ADR-0036, #369) |
| FR-23 | The customer shall add a payee and verify it by a confirmation-of-payee name check. Only verified payees can receive transfers. | UC-05 add, verify | Implemented | TASK-020 | Web `/payees` (`Payees.tsx`) |
| FR-24 | The customer shall edit a payee; changing the name forces re-verification. | UC-05 edit | Implemented | TASK-094 | `PATCH /api/v1/payees/{id}` and the edit dialog on `/payees` |
| FR-25 | The customer shall delete a payee. | UC-05 delete | Implemented | TASK-020 | Web `/payees` (`Payees.tsx`) |
| FR-26 | The customer shall pay a bill to a registered biller, idempotently and fraud-scored; a flagged payment is held for analyst review like a transfer. | Section 5 "bill payments" | Implemented | TASK-014, TASK-020 | Web `/bills` (`PayBills.tsx`) |
| FR-27 | The customer shall schedule one-off and recurring transfers, executed once per occurrence. | Section 5 "scheduled transfers" | Implemented | TASK-015, TASK-021 | API and web `/scheduled` (`ScheduledTransfers.tsx`, ADR-0023) |
| FR-28 | The customer shall see transaction history. | Section 5 "transaction history" | Implemented | TASK-008 | Per-account ledger history; there is no list of transfers as such |

### 2.4 Loans and wealth (UC-06, UC-07, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-29 | The system shall provide a loan catalogue and an EMI calculator with a repayment schedule. | Section 5 "loan catalogue, EMI calculator" | Implemented | TASK-045, TASK-051 | Web `/loans` (`Loans.tsx`) |
| FR-30 | The system shall assess loan eligibility from the customer's own balances, income, spending and existing repayments, with the reasons explained. | UC-06 "generates eligibility assessment" | Implemented | TASK-046; #470 | Five rules (ADR-0056): regular income, debt-to-income at most 50%, spending plus EMIs at most 90% of income, available balance covers the first EMI, no lockout in 90 days; reasons quote the customer's figures |
| FR-31 | The system shall display personalised loan suggestions. | UC-06 "displays recommendations" | Implemented | TASK-054, TASK-062; #470 | `GET /api/v1/loans/offers` and "Loans for you" on `/loans`; eligible catalogue loans with a suggested range on the carousel |
| FR-32 | The customer shall apply for a loan and track it. | Section 5 "loan applications, loan tracking" | Implemented | TASK-047, TASK-051 | API and web `/loans` (application, disbursal receipt, amortisation progress; ADR-0024) |
| FR-33 | The system shall record the customer's risk tolerance. | UC-07 input | Implemented | TASK-097 | Five-question questionnaire, deterministic LOW/MODERATE/HIGH band, answers and assessment time stored and audited (`/api/v1/customers/me/risk-profile`) |
| FR-34 | The system shall suggest investment products using risk tolerance, savings behaviour, income and available funds. | UC-07 | Implemented | TASK-054, TASK-097, TASK-105; #461 | Income, savings ratio, buffer and balance decide eligibility; the risk band is a suitability rule in the ranking (#461, ADR-0056) and in the assistant |
| FR-35 | The customer shall browse fixed deposits, mutual funds, stocks and bonds, and insurance products. | Section 5 wealth | Implemented | TASK-063, TASK-064, TASK-065, TASK-096; #465, #483, #484 | Fixed deposits with automatic payout; funds, stocks and bonds bought and sold at a simulated daily price through the ledger (#465) on `/wealth/invest` and the holdings (#483); monthly fund SIPs by an hourly job (#484) with "Start a monthly SIP" and "Your SIPs" screens (#487); simulated insurance quote and application with no premium debited (#483). Simulation is by the owner's direction (ADR-0058) |
| FR-36 | The customer shall compare wealth and insurance products. | Section 5 "product comparison" | Implemented | TASK-098 (merged #337) | `/wealth/compare`: two to four products side by side, "Not applicable" where a field does not apply |

### 2.5 AI assistant (UC-08, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-37 | The customer shall ask natural-language questions about their own balances, transactions, spending and forecast, in a chat drawer with streamed answers. | UC-08, section 5 "natural language queries", "spending analysis explanations" | Implemented | TASK-057 to TASK-061 | |
| FR-38 | The assistant shall answer "Can I afford a personal loan?" | UC-08 example | Implemented | TASK-057, TASK-046, TASK-110 (#284, merged #331) | Without an amount the assistant suggests the largest eligible amount or asks for one and says why (#331) |
| FR-39 | The assistant shall answer "Show unusual expenses." | UC-08 example | Implemented | TASK-110 (#284, merged #331) | |
| FR-40 | The assistant shall suggest investment options, support product discovery and answer FAQs. | UC-08 example, section 5 | Implemented | TASK-105 (#335); #474 | `get_faq_answer`, `list_products` and `get_investment_suggestions` (risk band and engine output). With #474 (in review): FAQ-2 covers every customer feature and `list_products` lists the simulated insurance plans |
| FR-41 | The assistant shall be read-only, act only on the authenticated customer's data, refuse money movement, other-customer, SQL and instruction-override requests, and audit every tool call. | Section 8 security (applied to UC-08) | Implemented | TASK-059, TASK-067, TASK-076 | |

### 2.6 Fraud (UC-09, UC-10, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-42 | The system shall produce a 0-100 fraud risk score with a decision, readable reasons and a model version for every transfer and bill payment, and persist the feature snapshot. | UC-09 outcome | Implemented | TASK-025, TASK-033 | |
| FR-43 | Trigger: large transfer amount. | UC-09 | Implemented | TASK-025 | `LARGE_AMOUNT`, `NEW_PAYEE_LARGE_AMOUNT` |
| FR-44 | Trigger: multiple login failures. | UC-09 | Implemented | TASK-095 | `RECENT_LOGIN_FAILURES` (3 or more in 24 h) |
| FR-45 | Trigger: new geographic location. | UC-09 | Partial | TASK-095; #143 | Country-level `NEW_COUNTRY`, new device/network, and city-level `IMPOSSIBLE_TRAVEL` (DB-IP City Lite, ADR-0034, #365); both need a GeoIP file and switch off without one (#370, #372); accuracy not evaluated (no labelled location data) |
| FR-46 | Trigger: rapid consecutive transactions. | UC-09 | Implemented | TASK-034 | `VELOCITY_BURST` (4 or more in 10 min) |
| FR-47 | Trigger: unusual transaction behaviour. | UC-09 | Implemented | TASK-030, TASK-033, #251 | `AMOUNT_SPIKE` in every mode; the Isolation Forest anomaly reason with `FRAUD_SCORER=hybrid`, which the local and demo configuration use since #251 (the code default and the staging compose stay `rules`) |
| FR-48 | A flagged transaction shall raise an alert, notify the customer, and appear in the analyst queue for resolution (release or reject). | UC-10 | Implemented | TASK-018, TASK-033, TASK-036 | |
| FR-49 | Analysts shall see suspicious-activity tracking and fraud statistics on a fraud monitoring dashboard. | Section 5 fraud dashboard | Implemented | TASK-035, TASK-036, TASK-099; #464 | `/staff/fraud-dashboard` charts `GET /fraud/stats`: decisions per day, hold and block rates, risk-score bands, top reasons, alert outcomes, masked destinations |

### 2.7 Analytics and recommendations (UC-11, UC-12, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-50 | The system shall capture page visits and clickstream events. | UC-11 data captured | Implemented | TASK-055; TASK-112 (#286, merged #333) | The ingestion API stores typed events in monthly partitions, at most 25 per batch with request p95 under 15 ms (ADR-0055, #458); the web app sends page visits as route templates and five allow-listed interactions (`docs/analytics/clickstream-events.md`) |
| FR-51 | The system shall capture session duration and product interests. | UC-11 data captured | Implemented | #463, #477 | Sessions split after 30 minutes of inactivity, durations and decayed product interests in a daily profile, for consenting customers only (ADR-0057); the customer sets consent on the Profile page (#477) |
| FR-52 | The system shall analyse spend and income patterns and assign each customer a behavioural segment. | UC-11, section 5 "customer segmentation" | Implemented | TASK-040, TASK-044, TASK-052 | |
| FR-53 | The system shall recommend loan, savings, investment and insurance products with reasons, using the behaviour profile and spending patterns. | UC-12 | Implemented | TASK-054, TASK-062; #461, #470 | `rules-v1` with segment, spending, income and held products; a ranker trained on simulated uptake re-orders only what the rules allow (ADR-0053, gate on simulated data only) |
| FR-54 | Recommendations shall use demographics. | UC-12 input | Implemented | TASK-107 | Limited by ADR-0015 to a consented age band derived from an optional date of birth; `unknown` without both |
| FR-55 | Recommendations shall take existing products into account. | UC-12 input | Implemented | TASK-111 (#285, merged #342) | Accounts, active loans and open fixed deposits are an input. A held savings account, fixed deposit or (with an active personal loan) the personal credit line is not offered again, except a fixed-deposit top-up when the balance covers at least 6 months of spending, whose reason says so (ADR-0030) |
| FR-56 | A relationship manager shall see customers ranked by churn risk, with signals and the top eligible product. | Section 6 (relationship manager) | Implemented | TASK-056; TASK-066; #347; #391 | RM page `/staff/insights` (TASK-066). Since #391 (merged in #397, ADR-0041) an RM's list opens in rule-based product score order (`order_by=PROPENSITY`), with churn-risk order one click away and the default for an administrator. Since #347 an RM sees only the customers an administrator assigned to them; any other customer is indistinguishable from a non-existent one (404); unassigned customers are visible to administrators only (ADR-0032) |
| FR-57 | Staff shall see a behavioural analytics dashboard: segmentation, spending patterns, product affinity, engagement. | Section 5 | Implemented | TASK-101 (merged #325) | Engagement is filled by the web app's clickstream events (TASK-112, merged #333); the seeded demo data has none |

### 2.8 Monitoring and compliance (UC-13, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-58 | Security staff shall see high-risk customers, active alerts, login anomalies, fraud events and open investigations. | UC-13 | Implemented | TASK-099 | `GET /api/v1/fraud/monitoring` and the `/staff/monitoring` page (fraud analysts and admins, with MFA); ids and aggregates only |
| FR-59 | A compliance officer shall read the audit history and verify its integrity. | Section 5 "audit history" | Implemented | TASK-048 | |
| FR-60 | Compliance shall see system-generated alerts, exception management and regulatory monitoring. | Section 5 | Implemented | TASK-100 (merged #325) | SLAs are the bank's own 24 h assumption (ADR-0027) |

## 3. Non-functional requirements (section 8)

| ID | Requirement | Status | Evidence | Tasks | Gap |
|---|---|---|---|---|---|
| NFR-01 | Secure authentication | Implemented | Argon2id, TOTP, rotating refresh, lockout; `test_auth.py` | TASK-006 | |
| NFR-02 | Encrypt sensitive data | Partial | MultiFernet field encryption with key rotation; `test_crypto.py`, `test_rotate_field_key.py`; TLS in nginx and to PostgreSQL (#280, `scripts/verify_tls.sh`); field-encryption policy for PII, ADR-0042 (#402) | TASK-075 | Certbot issuance on a real host (#198); #145 waits for #399 (the mobile number is field-encrypted since #428, closing #398) |
| NFR-03 | Role-based access control | Implemented | `require_roles`, policy functions; `test_rbac_bola.py`, route-policy registry test | | |
| NFR-04 | Audit logs | Implemented | Hash-chained append-only `audit_logs`; `test_audit_and_outbox.py` | | |
| NFR-05 | Protection against OWASP Top 10 | Implemented | Threat-model mapping, Bandit, CodeQL, dependency and secret scans, nightly Schemathesis, nightly OWASP ZAP baseline against the HTTPS stack | TASK-090 | The ZAP scan is passive and has not run against a deployed environment (#198) |
| NFR-06 | Page load < 2 s | Implemented | `apps/web/e2e/perf.spec.ts` in `perf.yml`: pages 110-451 ms in CI with 2 API replicas (#419); 313-1,600 ms on the Windows laptop (2026-11-04) | TASK-102 | Not measured on a deployed environment (#198) |
| NFR-07 | API response < 500 ms | Partial | k6 in CI with 2 API replicas: every route group p95 under 500 ms; fraud scoring p95 24.7 ms; assistant questions answered by the rules in under 0.1 s | TASK-078, TASK-102 | The assistant's model path for unclear questions takes 0.65-0.75 s with Groq on the demo stack (`engine=llm`, live runs on 5 Nov 2026) and 12-50 s with Ollama on CPU, the fallback (ADR-0018, ADR-0037); 500-user load and the host run wait for staging (TASK-078, TASK-079) |
| NFR-08 | Login < 3 s | Implemented | k6 login p95 340-406 ms and browser login 251-277 ms in CI (#419); browser login 2,133 ms on the Windows laptop (2026-11-04) | TASK-102 | Not measured on a deployed environment (#198) |
| NFR-09 | 99% availability in the test environment | Planned | Probes, availability ratio and alert, staging probe and report scripts exist | TASK-103, #186, #198 | Needs the staging host |
| NFR-10 | Horizontal scaling, stateless APIs, containerised deployment | Implemented | Stateless API, one process per container; two API replicas exercised in CI by `perf.yml` (#419, ADR-0045) | TASK-079 | Not yet run on the staging host |
| NFR-11 | Centralised logging, error tracking, health monitoring, performance metrics | Partial | Loki through Alloy, `unhandled_exception` events and metric, health probes, Prometheus and Grafana as code, worker metrics (ADR-0039), optional OTel collector | TASK-069, TASK-103 | No alert notification channel (no Alertmanager); not deployed with staging |

## 4. Engineering and documentation requirements (sections 9 to 11)

| ID | Requirement | Status | Tasks | Gap |
|---|---|---|---|---|
| ER-01 | Responsive web application | Implemented | | Screens for every use case (`apps/web/src/shell/routes.tsx`), including `/wealth/invest`, `/wealth/insurance` (#483) and the SIP screens (#487) |
| ER-02 | Backend API and database implementation | Implemented | | OpenAPI contract with the investing, insurance, suitability and SIP routes (#465, #483, #484); migrations 0001-0031; [ER diagram](../architecture/er-diagram.md) |
| ER-03 | API documentation | Implemented | | `docs/api/openapi.json`, drift-checked in CI |
| ER-04 | Automated tests, ≥ 70% coverage | Implemented | TASK-087 (report, #373; auth branch coverage 95.83% after #410) | CI coverage gate is 80% (backend); 96.71% in CI run 41689113122, 96.53% in run 41600683787 ([test strategy and report](../testing/test-strategy-and-report.md)) |
| ER-05 | CI: build, test, quality scan, security scan | Implemented | | |
| ER-06 | CI: package, deploy, smoke test | Partial | #198 | Packaging runs on every release tag; the deploy and smoke steps have not run against a host |
| ER-07 | Requirements specification, user stories, use case document | Implemented | TASK-104 | This document, [user-stories.md](user-stories.md) and [use-cases.md](use-cases.md) |
| ER-08 | HLD, LLD, ER diagram | Implemented | TASK-053, TASK-106, TASK-027 | [HLD](../architecture/HLD.md), [LLD](../architecture/LLD.md), [ER diagram](../architecture/er-diagram.md) (drift-checked in CI) and [diagrams](../diagrams/README.md) |
| ER-09 | Deployment guide, support guide | Implemented | TASK-075 | [Deployment guide](../operations/deployment-guide.md), [support guide](../operations/support-guide.md), [runbook](../operations/runbook.md) |
| ER-10 | User manual | Implemented | TASK-092 (#374) | [`docs/user-manual/`](../user-manual/README.md): customer, staff and compliance guides with screenshots |
| ER-11 | Peer reviews | Partial | | The lead reviews every PR before merge; teammates' area reviews are requested in [peer-review-requests.md](../testing/peer-review-requests.md); the ruleset requires 0 approvals |

## 5. Open gaps

Requirements that are not fully met, and what they wait for.

| Requirement | Missing part | Waits for |
|---|---|---|
| NFR-02 | At-rest encryption evidence on the deployment host | #399, #145 |
| NFR-06, NFR-07, NFR-08 | Budgets measured on staging; load at 500 users; the assistant's model path (0.65-0.75 s with Groq, 12-50 s with Ollama on CPU) | TASK-102 (#185), TASK-078 (#101); ADR-0018 and ADR-0037 record the assistant figures |
| NFR-09 | A month of availability on staging | TASK-103 (#186), #198 |
| NFR-10 | Scaling run on the host | TASK-079 |
| NFR-11 | An alert notification channel | owner decision |
| ER-06 | First deployment through the Deploy workflow | #198 |
| ER-11 | Recorded peer reviews | teammates |
