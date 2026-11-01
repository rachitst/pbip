# PBIP requirements specification

| | |
|---|---|
| Task | TASK-104 (#187) |
| Source | *PBIP Use Case Document v0.2*: sections 5 (scope), 6 (actors), 7 (UC-01 to UC-13), 8 (non-functional), 9 to 11 (deliverables, practices, success criteria) |
| Status checked against | `main` at `2e65b87` (1 Nov 2026): the code and the tests, not earlier documents |
| Companion documents | [User stories](user-stories.md) (acceptance criteria and tests per story), [traceability](traceability.md) (requirement to code, tests and gaps), [HLD](../architecture/HLD.md) |

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
| A2 | Relationship manager | `RELATIONSHIP_MANAGER` |
| A3 | Fraud analyst | `FRAUD_ANALYST` |
| A4 | Compliance officer | `COMPLIANCE_OFFICER` |
| A5 | Banking administrator | `ADMIN` |
| A6 | AI assistant | Not a role. It acts only on behalf of the authenticated customer, through read-only tools |

Every staff role (A2 to A5) must have completed TOTP in the current session to call staff endpoints.

## 2. Functional requirements

### 2.1 Access and identity (UC-01)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-01 | The system shall authenticate a customer with an e-mail and password. | UC-01 flow 1-2 | Implemented | TASK-006 | |
| FR-02 | The system shall support TOTP multi-factor authentication with single-use recovery codes. A customer with MFA enabled must enter a code before access is granted. | UC-01 flow 3 | Partial | TASK-006; no task for UI | MFA is optional for customers (mandatory for staff); there is no MFA-enrolment screen in the web app |
| FR-03 | The system shall record a security event for every failed login and MFA failure, and lock the account progressively after repeated failures. | UC-01 alternative flow | Implemented | TASK-006, TASK-007 | |
| FR-04 | The system shall issue short-lived access tokens and rotating refresh tokens, revoke a token family on reuse, and revoke all sessions on password change. | UC-01, section 8 "secure authentication" | Implemented | TASK-006 | |
| FR-05 | An administrator shall be able to create staff users with explicit roles and inspect and clear a user's lockout. Each action is audited. | Section 6 (administrator) | Partial | TASK-007; TASK-108 (#282) | API only; no administration screen; no application-configuration function |
| FR-06 | A retail customer shall be able to self-register (disabled in production by configuration). | UC-01 precondition "valid customer account exists" | Partial | no task for UI | API only; the web app has no registration page |

### 2.2 Dashboard and account services (UC-02, UC-03, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-07 | After login, the customer shall see every account with its available, ledger and held balance. | UC-02, section 5 "account summaries" | Implemented | TASK-019 | |
| FR-08 | The dashboard shall show the total balance across the customer's accounts. | Section 5 "total balance overview" | Planned | TASK-109 (#283) | The total is computed for recommendations but not shown |
| FR-09 | The customer shall see recent transactions for an account, paginated. | UC-02, section 5 | Implemented | TASK-008, TASK-019 | |
| FR-10 | The customer shall see spending insights: category breakdown, month-over-month trends, daily burn rate. | UC-02, section 5 "spending insights" | Implemented | TASK-040, TASK-049 | |
| FR-11 | The customer shall see financial health indicators: a 0-100 health score with components and tips, and a balance forecast labelled as a prediction. | Section 5 "financial health indicators" | Partial | TASK-041, TASK-042, TASK-049; TASK-050 | The forecast has an API but no chart |
| FR-12 | The dashboard shall show personalised product recommendations. | UC-02, section 5 | Partial | TASK-054; TASK-062 | API only; no UI |
| FR-13 | The customer shall be able to open an account and view its details. | Section 5 "view account details" | Implemented | TASK-005 | |
| FR-14 | The customer shall be able to search and filter transactions by date and amount. | Section 5 "transaction search" | Planned | TASK-017 | |
| FR-15 | The customer shall be able to view a statement for a chosen account and period and download it as PDF. | UC-03, section 5 | Planned | TASK-016, TASK-074 | `modules/statements` is empty |
| FR-16 | The customer shall be able to manage account settings: view the profile, list and register devices, change the password. | Section 5 "account settings management" | Implemented | TASK-004, TASK-006 | |
| FR-17 | The customer shall receive in-app notifications (transfer posted, under review, declined; bill payment declined) with an unread badge, and can mark them read. | UC-10 "customer is notified" | Implemented | TASK-018, TASK-022 | |

### 2.3 Payments and transfers (UC-04, UC-05, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-18 | The customer shall transfer money from a chosen source account to a verified payee. The steps are: choose source and payee, enter an amount, review, confirm. | UC-04 flow | Implemented | TASK-037 | |
| FR-19 | A transfer shall be refused when the available balance is insufficient, and leave no partial state. | UC-04 validation "sufficient balance" | Implemented | baseline ledger and payments (before the backlog), TASK-005 | |
| FR-20 | A transfer shall be refused above the per-transfer limit (₹2,00,000) or above the daily limit (₹5,00,000 per UTC day, shared with bill payments). | UC-04 validation "transfer limits" | Implemented | baseline payments (before the backlog), TASK-014 | |
| FR-21 | Every transfer shall be fraud-scored before posting. ALLOW posts it; STEP_UP requires a TOTP code (3 wrong codes, or no MFA enrolled, hand it to an analyst); REVIEW holds the funds for an analyst; BLOCK declines it. The customer never sees the reasons. | UC-04 validation "fraud checks" | Implemented | TASK-025, TASK-033 | |
| FR-22 | Money-moving requests shall be idempotent: a retry with the same key returns the original result and never moves money twice. | UC-04 outcome "successfully submitted" (interpreted for safe retries) | Implemented | baseline payments (before the backlog), TASK-014 | |
| FR-23 | The customer shall add a payee and verify it by a confirmation-of-payee name check. Only verified payees can receive transfers. | UC-05 add, verify | Partial | TASK-020 | API only; no UI |
| FR-24 | The customer shall edit a payee; changing the name forces re-verification. | UC-05 edit | Planned | TASK-094 | |
| FR-25 | The customer shall delete a payee. | UC-05 delete | Partial | TASK-020 | API only; no UI |
| FR-26 | The customer shall pay a bill to a registered biller, idempotently and fraud-scored; a flagged payment is held for analyst review like a transfer. | Section 5 "bill payments" | Partial | TASK-014; TASK-020 | API only; no UI |
| FR-27 | The customer shall schedule one-off and recurring transfers, executed once per occurrence. | Section 5 "scheduled transfers" | Planned | TASK-015, TASK-021 | |
| FR-28 | The customer shall see transaction history. | Section 5 "transaction history" | Implemented | TASK-008 | Per-account ledger history; there is no list of transfers as such |

### 2.4 Loans and wealth (UC-06, UC-07, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-29 | The system shall provide a loan catalogue and an EMI calculator with a repayment schedule. | Section 5 "loan catalogue, EMI calculator" | Partial | TASK-045; TASK-051 | API only; no UI |
| FR-30 | The system shall assess loan eligibility from the customer's own income and existing repayments, with the reasons explained. | UC-06 "generates eligibility assessment" | Partial | TASK-046 | Uses income and EMIs (DTI ≤ 50%), not balances or spending patterns as UC-06 lists |
| FR-31 | The system shall display personalised loan suggestions. | UC-06 "displays recommendations" | Partial | TASK-054, TASK-062 | The recommendation engine suggests a credit line and a rewards card, not the catalogue loans; no UI |
| FR-32 | The customer shall apply for a loan and track it. | Section 5 "loan applications, loan tracking" | Planned | TASK-047, TASK-051 | |
| FR-33 | The system shall record the customer's risk tolerance. | UC-07 input | Planned | TASK-097 | |
| FR-34 | The system shall suggest investment products using risk tolerance, savings behaviour, income and available funds. | UC-07 | Partial | TASK-054; TASK-097, TASK-105 | Fixed deposit and index-fund SIP suggestions use income, savings ratio, buffer and balance; no risk tolerance |
| FR-35 | The customer shall browse fixed deposits, mutual funds, stocks and bonds, and insurance products. | Section 5 wealth | Planned | TASK-063, TASK-064, TASK-065, TASK-096 | `modules/wealth` is empty |
| FR-36 | The customer shall compare wealth and insurance products. | Section 5 "product comparison" | Planned | TASK-098 | |

### 2.5 AI assistant (UC-08, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-37 | The customer shall ask natural-language questions about their own balances, transactions, spending and forecast, in a chat drawer with streamed answers. | UC-08, section 5 "natural language queries", "spending analysis explanations" | Implemented | TASK-057 to TASK-061 | |
| FR-38 | The assistant shall answer "Can I afford a personal loan?" | UC-08 example | Partial | TASK-057 | Answered only when the customer states an amount; with no amount it replies with help text |
| FR-39 | The assistant shall answer "Show unusual expenses." | UC-08 example | Planned | TASK-110 (#284) | It currently returns the spending total; there is no anomaly tool |
| FR-40 | The assistant shall suggest investment options, support product discovery and answer FAQs. | UC-08 example, section 5 | Planned | TASK-105 | |
| FR-41 | The assistant shall be read-only, act only on the authenticated customer's data, refuse money movement, other-customer, SQL and instruction-override requests, and audit every tool call. | Section 8 security (applied to UC-08) | Implemented | TASK-059, TASK-067, TASK-076 | |

### 2.6 Fraud (UC-09, UC-10, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-42 | The system shall produce a 0-100 fraud risk score with a decision, readable reasons and a model version for every transfer and bill payment, and persist the feature snapshot. | UC-09 outcome | Implemented | TASK-025, TASK-033 | |
| FR-43 | Trigger: large transfer amount. | UC-09 | Implemented | TASK-025 | `LARGE_AMOUNT`, `NEW_PAYEE_LARGE_AMOUNT` |
| FR-44 | Trigger: multiple login failures. | UC-09 | Implemented | TASK-095 | `RECENT_LOGIN_FAILURES` (3 or more in 24 h) |
| FR-45 | Trigger: new geographic location. | UC-09 | Partial | TASK-095 | Country-level (`NEW_COUNTRY`, needs the GeoIP file) and new device/network only; no finer location; accuracy not evaluated |
| FR-46 | Trigger: rapid consecutive transactions. | UC-09 | Implemented | TASK-034 | `VELOCITY_BURST` (4 or more in 10 min) |
| FR-47 | Trigger: unusual transaction behaviour. | UC-09 | Implemented | TASK-030, TASK-033, #251 | `AMOUNT_SPIKE` in every mode; the Isolation Forest anomaly reason with `FRAUD_SCORER=hybrid`, which the local and demo configuration use since #251 (the code default and the staging compose stay `rules`) |
| FR-48 | A flagged transaction shall raise an alert, notify the customer, and appear in the analyst queue for resolution (release or reject). | UC-10 | Implemented | TASK-018, TASK-033, TASK-036 | |
| FR-49 | Analysts shall see suspicious-activity tracking and fraud statistics on a fraud monitoring dashboard. | Section 5 fraud dashboard | Partial | TASK-035, TASK-036; TASK-099 | Alert queue and detail screens and the statistics API (decisions over time, top reasons, masked risky payees) exist; the statistics are not charted in the UI |

### 2.7 Analytics and recommendations (UC-11, UC-12, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-50 | The system shall capture page visits and clickstream events. | UC-11 data captured | Partial | TASK-055; TASK-112 (#286) | The ingestion API stores typed events in monthly partitions; the web app does not send any |
| FR-51 | The system shall capture session duration and product interests. | UC-11 data captured | Planned | TASK-055, TASK-101 | No session-duration field; no derived product interest |
| FR-52 | The system shall analyse spend and income patterns and assign each customer a behavioural segment. | UC-11, section 5 "customer segmentation" | Implemented | TASK-040, TASK-044, TASK-052 | |
| FR-53 | The system shall recommend loan, savings, investment and insurance products with reasons, using the behaviour profile and spending patterns. | UC-12 | Partial | TASK-054; TASK-062 | Rules-based (`rules-v1`) API; no UI |
| FR-54 | Recommendations shall use demographics. | UC-12 input | Planned | TASK-107 | Limited by ADR-0015 to a consented age band; currently always `unknown` |
| FR-55 | Recommendations shall take existing products into account. | UC-12 input | Planned | TASK-111 (#285) | Products already held are not an input |
| FR-56 | A relationship manager shall see customers ranked by churn risk, with signals and the top eligible product. | Section 6 (relationship manager) | Partial | TASK-056; TASK-066 | API only; no UI |
| FR-57 | Staff shall see a behavioural analytics dashboard: segmentation, spending patterns, product affinity, engagement. | Section 5 | Planned | TASK-101 | |

### 2.8 Monitoring and compliance (UC-13, section 5)

| ID | Requirement | Source | Status | Tasks | Gap |
|---|---|---|---|---|---|
| FR-58 | Security staff shall see high-risk customers, active alerts, login anomalies, fraud events and open investigations. | UC-13 | Implemented | TASK-099 | `GET /api/v1/fraud/monitoring` and the `/staff/monitoring` page (fraud analysts and admins, with MFA); ids and aggregates only |
| FR-59 | A compliance officer shall read the audit history and verify its integrity. | Section 5 "audit history" | Implemented | TASK-048 | |
| FR-60 | Compliance shall see system-generated alerts, exception management and regulatory monitoring. | Section 5 | Planned | TASK-100 | |

## 3. Non-functional requirements (section 8)

| ID | Requirement | Status | Evidence | Tasks | Gap |
|---|---|---|---|---|---|
| NFR-01 | Secure authentication | Implemented | Argon2id, TOTP, rotating refresh, lockout; `test_auth.py` | TASK-006 | |
| NFR-02 | Encrypt sensitive data | Partial | MultiFernet field encryption with key rotation; `test_crypto.py`, `test_rotate_field_key.py` | TASK-075 | TLS is terminated outside the repo (runbook section 1.4); no task |
| NFR-03 | Role-based access control | Implemented | `require_roles`, policy functions; `test_rbac_bola.py`, route-policy registry test | | |
| NFR-04 | Audit logs | Implemented | Hash-chained append-only `audit_logs`; `test_audit_and_outbox.py` | | |
| NFR-05 | Protection against OWASP Top 10 | Partial | Threat-model mapping, Bandit, CodeQL, nightly Schemathesis | TASK-090 | No DAST against a deployed environment |
| NFR-06 | Page load < 2 s | Planned | Not measured | TASK-102 | |
| NFR-07 | API response < 500 ms | Partial | Analytics endpoints p95 12-19 ms (local, sequential); fraud scoring p95 24.7 ms; the assistant's LLM path for unclear questions takes 12-50 s on CPU | TASK-078, TASK-102 | Not measured under load; LLM path misses the target (documented in ADR-0018) |
| NFR-08 | Login < 3 s | Planned | Not measured | TASK-102 | |
| NFR-09 | 99% availability in the test environment | Planned | No environment deployed | TASK-103, #198 | |
| NFR-10 | Horizontal scaling, stateless APIs, containerised deployment | Partial | Stateless API, one worker per container, Docker images and compose | TASK-079 | Multi-replica deployment never exercised |
| NFR-11 | Centralised logging, error tracking, health monitoring, performance metrics | Partial | structlog → Alloy → Loki, Prometheus metrics and alert rules, Grafana, health probes (dev compose) | TASK-069, TASK-103 | No error tracking; no Alertmanager; not deployed |

## 4. Engineering and documentation requirements (sections 9 to 11)

| ID | Requirement | Status | Tasks | Gap |
|---|---|---|---|---|
| ER-01 | Responsive web application | Partial | Remaining UI tasks above | Most staff journeys and several customer journeys have no screen |
| ER-02 | Backend API and database implementation | Implemented | | 57 operations, migrations 0001-0010, [ER diagram](../architecture/er-diagram.md) |
| ER-03 | API documentation | Implemented | | `docs/api/openapi.json`, drift-checked in CI |
| ER-04 | Automated tests, ≥ 70% coverage | Implemented | TASK-087 (report) | CI coverage gate is 80% (backend) |
| ER-05 | CI: build, test, quality scan, security scan | Implemented | | |
| ER-06 | CI: package, deploy, smoke test | Partial | #198, TASK-091 | Deploy workflow exists but is gated off; no staging host |
| ER-07 | Requirements specification, user stories, use case document | Implemented | TASK-104 | This document and [user-stories.md](user-stories.md) |
| ER-08 | HLD, LLD, ER diagram | Partial | TASK-053 (HLD), TASK-106 (ER, done), TASK-027 (LLD) | LLD waits for TASK-015 |
| ER-09 | Deployment guide, support guide | Implemented | TASK-013, TASK-075 | |
| ER-10 | User manual | Planned | TASK-092 | Waits for the remaining UI |
| ER-11 | Peer reviews | Partial | | The ruleset requires 0 approvals (see traceability) |

## 5. Gaps with no owning task

These requirements were Partial or Planned with no backlog task for the missing part. On 2026-11-01 each one got a task
(TASK-108 to TASK-112, unassigned and unscheduled, plan section 6a), was given to its existing owner (#201), or was
delivered (TLS, #280).

| Requirement | Missing part |
|---|---|
| FR-02, FR-06 | Web screens for MFA enrolment and self-registration (owned by #201) |
| FR-05 | Administration screen and application-configuration function (TASK-108, #282) |
| FR-08 | Total-balance overview on the dashboard (TASK-109, #283) |
| FR-39 | "Show unusual expenses" in the assistant (TASK-110, #284) |
| FR-55 | Existing products as a recommendation input (TASK-111, #285) |
| FR-50 | The web app sending page visits and clickstream events (TASK-112, #286) |
| NFR-02 | ~~TLS termination as code~~: done in #280 (nginx TLS + HSTS, PostgreSQL `verify-full`); certbot issuance on a real host waits for #198 |
