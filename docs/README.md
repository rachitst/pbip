# PBIP documentation

Every document in the repository, by audience. The [project README](../README.md) is the product overview and quick
start. When documents disagree, the code on `main` is the authority, then [AGENTS.md](../AGENTS.md) for engineering
rules and the [blueprint](PROJECT_BLUEPRINT.md) for intent.

## Business and requirements

| Document | What it covers |
|---|---|
| [Requirements specification](requirements/requirements-specification.md) | Functional and non-functional requirements derived from the use-case document |
| [User stories](requirements/user-stories.md) | UC-01 to UC-13 as user stories with acceptance criteria and the tests that prove them |
| [Traceability matrix](requirements/traceability.md) | Every use case, scope item and non-functional requirement mapped to code, tests and remaining gaps |
| [Use case diagram](diagrams/use-case.md) | The actors and UC-01 to UC-13 with the routes behind each |
| [Clickstream event catalogue](analytics/clickstream-events.md) | What the web app sends for behaviour analytics, and what it never sends |

## Technical design

| Document | What it covers |
|---|---|
| [Engineering blueprint](PROJECT_BLUEPRINT.md) | Architecture, engineering rules, scope and the plan the system was built to |
| [High-level design](architecture/HLD.md) | System boundary, C4 context and containers, components, contracts, security, scalability, roadmap |
| [Low-level design](architecture/LLD.md) | Every table with its constraints and triggers, state machines, lock-order and idempotency sequences |
| [ER diagram](architecture/er-diagram.md) | Generated from the migrated schema and drift-checked in CI |
| [Diagrams](diagrams/README.md) | Use case, activity, sequence, state, module, component, deployment and data flow diagrams, with SVG and PNG exports |
| [Architecture decision records](adr/) | One record per decision with trade-offs, from ADR-0001 (modular monolith) onwards |
| [API contract](api/openapi.json) | The generated OpenAPI document; a running API also serves it at `/docs` |
| [UI guidelines and design system](design/ui-guidelines.md) | Layout, components, accessibility and theming rules for the web app (ADR-0016) |

## Security

| Document | What it covers |
|---|---|
| [Threat model](security/threat-model.md) | Assets, trust boundaries, threats with their controls and residual risks, OWASP Top 10 mapping |
| [AI assistant security](security/ai-assistant-security.md) | Tool registry, prompt-injection defences, data that may and may not reach a model |
| [Security audit, November 2026](security/security-audit-2026-11.md) | Pre-release static analysis, dependency audit, secret scan, ZAP baseline and audit-chain check |
| [Risk acceptances](security/risk-acceptances.md) | Risks the product owner accepted, with the reason, the exposure and what reopens each |
| [DAST report](security/zap-2026-11-01/) | OWASP ZAP baseline scan of the full stack |

## Machine learning

| Document | What it covers |
|---|---|
| [Fraud models](ml/fraud-models.md) | Features, rules, LightGBM, Isolation Forest, the hybrid combiner and how it is served |
| [Model evaluation report](ml/model-evaluation-report-v1.md) | Time-based evaluation: precision, recall, PR-AUC, error rates, latency, confusion matrices, limitations |
| [Model cards](ml/model-cards.md) | One card per model, and the AI governance alignment |
| [Balance forecast](ml/balance-forecast.md) | The end-of-month projection method and its limits |
| [Recommendation propensity decision](ml/recommendation-propensity-decision.md) | Why recommendations rank with rules first and how the simulated-uptake ranker is gated |
| [Circular transfer spike](ml/circular-transfer-spike.md) | Offline investigation of mule rings (not served) |

## Testing and quality

| Document | What it covers |
|---|---|
| [Test strategy and report](testing/test-strategy-and-report.md) | Test pyramid, coverage, concurrency, performance, security and ML testing, the code review process |
| [End-to-end tests](testing/e2e.md) | The Playwright journeys and how to run them |
| [Mutation testing](testing/mutation-report.md) | Mutation score of the ledger and payments code |
| [API fuzzing](testing/fuzzing.md) | Schemathesis property-based fuzzing |
| Performance | [1 Nov](testing/performance-2026-11-01.md) and [4 Nov](testing/performance-2026-11-04.md) runs; [analytics latency](testing/analytics-latency-2026-11-04.md); [clickstream latency](testing/events-latency-2026-11-04.md) |
| [Local verification, 30 Oct](testing/local-verification-2026-10-30.md) | A full local verification run and its results |

## Operations

| Document | What it covers |
|---|---|
| [Deployment guide](operations/deployment-guide.md) | Entry point: prerequisites, release tag to staging, rollback, the model files |
| [Support guide](operations/support-guide.md) | L1 and L2 triage, common issues with diagnosis and fix, escalation, where logs and dashboards are |
| [Runbook](operations/runbook.md) | Deploy, roll back, secret rotation, backup and restore, incident playbooks |
| [Oracle Cloud Arm staging guide](operations/deploy-oracle-cloud-arm.md) | Building the staging host, from the cloud account to the first deployment |
| [Private-repository checklist](operations/private-repo-checklist.md) | What changes if the repository becomes private |
| [Setup guide](SETUP_GUIDE.md) | Local setup on Ubuntu, macOS and native Windows |

## Using PBIP

| Document | What it covers |
|---|---|
| [User manual](user-manual/README.md) | Overview and demo users |
| [Customer banking guide](user-manual/customer-guide.md) | Every customer screen, step by step |
| [Staff operations guide](user-manual/staff-guide.md) | Fraud analysts, relationship managers and administrators |
| [Compliance guide](user-manual/compliance-guide.md) | Compliance dashboard, exceptions and the audit log |
| [Demo script](../demo/demo-script.md) | The live demonstration and its fallback plan |

## Project and releases

| Document | What it covers |
|---|---|
| [Contributing](../CONTRIBUTING.md) | Branches, commits, pull requests, review and merge |
| [AGENTS.md](../AGENTS.md) | Binding engineering rules for people and AI coding assistants |
| [Changelog](../CHANGELOG.md) | Every release and what changed |
| [Release notes](release/) | One file per release; the latest is [v1.0.2](release/v1.0.2.md) |
| [Team execution plan](TEAM_EXECUTION_PLAN.md) | Owners, dates and status of every task |
| [Backlog](backlog/) | Tasks with acceptance criteria and definitions of done |

Rule: a pull request that changes behaviour updates the relevant document in the same PR (AGENTS.md section 12).
`python scripts/check_links.py` checks that every relative link and image in these documents resolves; CI runs it.
