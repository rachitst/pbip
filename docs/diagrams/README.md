# Diagrams

UML-style and data flow diagrams of PBIP **as built on `main`**. Every name, state and flow is taken from the code
(enums, routers, services, the Celery beat schedule and the compose files); each diagram names its source files.
The Mermaid source lives in the Markdown files below, so GitHub renders it; SVG and PNG exports sit next to it in
[`svg/`](svg/) and [`png/`](png/) for documents and slides.

Other diagrams that already exist and stay where they are:

* [C4 system context and containers](../architecture/HLD.md#2-c4-level-1-system-context) in the HLD;
* [ER diagram](../architecture/er-diagram.md), generated from the migrated schema by `scripts/er_diagram.py` and
  drift-checked in CI;
* [state machines with their transition tables](../architecture/LLD.md#4-state-machines) and the
  [lock-order and idempotency sequences](../architecture/LLD.md#5-sequences) in the LLD.

## Index

| Diagram | Kind | What it shows | Exports |
|---|---|---|---|
| [Use cases](use-case.md) | use case | The seven actors and UC-01 to UC-13, with the routes behind each | [SVG](svg/use-case.svg), [PNG](png/use-case.png) |
| [Login with MFA](activity.md#1-login-with-mfa) | activity | Password check, lockout, MFA challenge, TOTP or recovery code, session issue | [SVG](svg/activity-login-mfa.svg), [PNG](png/activity-login-mfa.png) |
| [Transfer](activity.md#2-transfer-with-review-step-up-and-fraud-decision) | activity | Review, idempotent create, fraud decision, step-up, escalation and expiry | [SVG](svg/activity-transfer.svg), [PNG](png/activity-transfer.png) |
| [Fraud alert management](activity.md#3-fraud-alert-management-uc-10) | activity | UC-10: alert opened, customer notified, analyst release or reject | [SVG](svg/activity-fraud-alert.svg), [PNG](png/activity-fraud-alert.png) |
| [Loan application and disbursal](activity.md#4-loan-application-and-disbursal) | activity | EMI, eligibility, apply, disbursal and scheduled repayments | [SVG](svg/activity-loan.svg), [PNG](png/activity-loan.png) |
| [Bill payment](activity.md#5-bill-payment) | activity | Biller checks, fraud decision, hold for review | [SVG](svg/activity-bill-payment.svg), [PNG](png/activity-bill-payment.png) |
| [Scheduled transfer run](activity.md#6-scheduled-transfer-run) | activity | The 60-second beat job: missed dates, retries, pauses, completion | [SVG](svg/activity-scheduled-run.svg), [PNG](png/activity-scheduled-run.png) |
| [Fixed deposit booking and maturity payout](activity.md#7-fixed-deposit-booking-and-maturity-payout) | activity | Booking, early closure and the hourly exactly-once payout | [SVG](svg/activity-fixed-deposit.svg), [PNG](png/activity-fixed-deposit.png) |
| [Investing: buy and sell](activity.md#8-investing-buy-and-sell) | activity | Simulated prices, holding lock, ledger purchase and sale (PR #465) | [SVG](svg/activity-investing.svg), [PNG](png/activity-investing.png) |
| [Behavioural profile and analytics consent](activity.md#9-behavioural-profile-and-analytics-consent-uc-11) | activity | Consent, erase on withdrawal, consent-gated clickstream, daily profile job (#463) | [SVG](svg/activity-behaviour-profile.svg), [PNG](png/activity-behaviour-profile.png) |
| [Loan recommendations](activity.md#10-loan-recommendations-uc-06) | activity | Catalogue offers on income, spending, balance and lockout rules (PR #470) | [SVG](svg/activity-loan-offers.svg), [PNG](png/activity-loan-offers.png) |
| [Fraud monitoring dashboard](activity.md#11-fraud-monitoring-dashboard) | activity | `/fraud/stats` aggregates, risk bands, alert outcomes, 60 s cache (PR #464) | [SVG](svg/activity-fraud-dashboard.svg), [PNG](png/activity-fraud-dashboard.png) |
| [Compliance exceptions](activity.md#12-compliance-exceptions) | activity | Five exception sources, acknowledge and resolve through the audit log (PR #466) | [SVG](svg/activity-compliance-exceptions.svg), [PNG](png/activity-compliance-exceptions.png) |
| [Transfer with hybrid fraud scoring](sequence.md#1-transfer-with-hybrid-fraud-scoring) | sequence | Lock order, rules then model within budget, rules-only fail-safe | [SVG](svg/sequence-transfer-hybrid-scoring.svg), [PNG](png/sequence-transfer-hybrid-scoring.png) |
| [Step-up verification](sequence.md#2-step-up-verification) | sequence | TOTP confirm, wrong codes, escalation to review | [SVG](svg/sequence-step-up.svg), [PNG](png/sequence-step-up.png) |
| [Assistant](sequence.md#3-assistant-question-to-answer) | sequence | Guard, rules route, Groq, Ollama, rules fallback, read-only tool, redacted reply | [SVG](svg/sequence-assistant.svg), [PNG](png/sequence-assistant.png) |
| [Statement PDF download](sequence.md#4-statement-pdf-download) | sequence | Ownership check, period limits, PDF render, audit and notification | [SVG](svg/sequence-statement.svg), [PNG](png/sequence-statement.png) |
| [Token refresh and revocation](sequence.md#5-token-refresh-and-revocation-adr-0021) | sequence | Refresh rotation, reuse detection, token version check (ADR-0021) | [SVG](svg/sequence-token-refresh.svg), [PNG](png/sequence-token-refresh.png) |
| [Transaction lifecycle](state.md#1-transaction-lifecycle) | state | INITIATED, PENDING_STEP_UP, PENDING_REVIEW, POSTED, DECLINED, REVERSED | [SVG](svg/state-transaction.svg), [PNG](png/state-transaction.png) |
| [Fraud alert lifecycle](state.md#2-fraud-alert-lifecycle) | state | OPEN with severity, FALSE_POSITIVE, CONFIRMED_FRAUD | [SVG](svg/state-fraud-alert.svg), [PNG](png/state-fraud-alert.png) |
| [Loan lifecycle](state.md#3-loan-lifecycle) | state | Loan ACTIVE to CLOSED, instalment DUE to PAID | [SVG](svg/state-loan.svg), [PNG](png/state-loan.png) |
| [Fixed deposit lifecycle](state.md#4-fixed-deposit-lifecycle) | state | ACTIVE, derived MATURED, payout held, CLOSED by kind | [SVG](svg/state-fixed-deposit.svg), [PNG](png/state-fixed-deposit.png) |
| [Investment holding](state.md#5-investment-holding) | state | Zero, held and sold-out positions; BUY and SELL orders | [SVG](svg/state-investment-holding.svg), [PNG](png/state-investment-holding.png) |
| [Compliance exception](state.md#6-compliance-exception) | state | OPEN, ACKNOWLEDGED, RESOLVED, read from the audit log | [SVG](svg/state-compliance-exception.svg), [PNG](png/state-compliance-exception.png) |
| [Analytics consent](state.md#7-analytics-consent) | state | Not decided, granted, withdrawn (erases data) | [SVG](svg/state-analytics-consent.svg), [PNG](png/state-analytics-consent.png) |
| [Fund SIP set-up and instalment](activity.md#13-fund-sip-set-up-and-monthly-instalment) | activity | Set-up without money, hourly exactly-once instalment, failures moved to next month | [SVG](svg/activity-fund-sip.svg), [PNG](png/activity-fund-sip.png) |
| [Insurance quote and application](activity.md#14-insurance-quote-and-application) | activity | Simulated decision rules, premium factors, no ledger | [SVG](svg/activity-insurance-application.svg), [PNG](png/activity-insurance-application.png) |
| [Fund SIP lifecycle](state.md#8-fund-sip-and-its-instalments) | state | ACTIVE, CANCELLED; instalment EXECUTED or FAILED | [SVG](svg/state-fund-sip.svg), [PNG](png/state-fund-sip.png) |
| [Insurance application](state.md#9-insurance-application) | state | DECLINED, SUBMITTED, APPROVED_SIMULATED | [SVG](svg/state-insurance-application.svg), [PNG](png/state-insurance-application.png) |
| [Layers inside a module](module.md#1-layers-inside-a-module) | class | router, service, policy, repository, models, schemas | [SVG](svg/module-layers.svg), [PNG](png/module-layers.png) |
| [Module dependencies](module.md#2-module-dependencies) | module | Which API module imports which | [SVG](svg/module-dependencies.svg), [PNG](png/module-dependencies.png) |
| [Import contracts](module.md#3-import-contracts) | module | The five import-linter contracts CI enforces | [SVG](svg/module-import-contracts.svg), [PNG](png/module-import-contracts.png) |
| [Components](component.md) | component | Web, nginx, API, worker and beat, PostgreSQL, Redis, ML package, LLM providers, monitoring | [SVG](svg/component.svg), [PNG](png/component.png) |
| [Docker Compose stack](deployment.md#1-docker-compose-stack) | deployment | Local services, ports, volumes and profiles | [SVG](svg/deployment-compose.svg), [PNG](png/deployment-compose.png) |
| [Oracle Cloud Arm staging](deployment.md#2-oracle-cloud-arm-staging) | deployment | Release tag, GHCR, the VM, TLS, probe and backups | [SVG](svg/deployment-oracle-arm.svg), [PNG](png/deployment-oracle-arm.png) |
| [Data flow level 0](data-flow.md#level-0-context) | data flow | PBIP and its external entities | [SVG](svg/dfd-level-0.svg), [PNG](png/dfd-level-0.png) |
| [Data flow level 1](data-flow.md#level-1-processes-and-data-stores) | data flow | Ten processes and ten data stores | [SVG](svg/dfd-level-1.svg), [PNG](png/dfd-level-1.png) |

## Regenerating the exports

```bash
bash scripts/render_diagrams.sh                       # every diagram
bash scripts/render_diagrams.sh state-loan use-case    # only the named ones
```

The script installs the pinned `@mermaid-js/mermaid-cli` from [`package-lock.json`](package-lock.json) on first use
(`npm ci` in this folder), splits every Markdown file here into one Mermaid file per diagram, and writes
`svg/<name>.svg` and `png/<name>.png`. Puppeteer does not download its own browser
([`.puppeteerrc.cjs`](.puppeteerrc.cjs)); the script points it at the Chromium that Playwright installs for the E2E
suite (`cd apps/web && npx playwright install chromium`), or at `PBIP_CHROME_PATH` if set.

This folder is **docs tooling only**: its `package.json` is not part of the web build, the Docker images or any
production artefact, and nothing in CI installs it.

## Adding or changing a diagram

1. Edit the Mermaid block in the Markdown file. Each block is preceded by a marker comment,
   `<!-- diagram: <name> -->`, which names its export files; a new diagram needs a new, unique name.
2. Take every state, route and job name from the code, and name the source files in the section text.
3. Run `bash scripts/render_diagrams.sh <name>` and commit the Markdown together with the new SVG and PNG. Render only
   the diagrams you changed: the exports are not byte-for-byte reproducible (Mermaid generates element ids), so
   re-rendering an unchanged diagram only adds noise to the diff.
4. Add a row to the index above.

Every file ends with an **extension point** paragraph that says where a new use case, flow, state, module or service
belongs in that diagram.
