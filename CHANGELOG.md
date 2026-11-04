# Changelog

All notable changes to PBIP are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/). Numbers in brackets are the pull requests that made
each change. The plain-language summary of this release is in [`docs/release/v1.0.0.md`](docs/release/v1.0.0.md).

## [1.0.0] - 2026-11-04

The first release, tagged `v1.0.0` on `main` `9ffbfa8`.

### Features

#### Banking core
- Customer profile and device registration, with atomic first registration of a device (#27, #36).
- Account opening with ledger-owned initialisation, a double-entry ledger with immutable entries, balanced postings
  and idempotent, replay-safe money-moving endpoints (#28, #36).
- Password change that signs out every session, admin lockout view and unlock, keyset-paginated transaction history
  (#36).
- Transaction filter and search on the keyset cursor (#303).
- Total balance overview per currency on the customer dashboard (#330).
- Customers can set and delete a mobile number, stored encrypted and shown masked (#428, #398).
- Customer notifications inbox with read status, a real-time notification bell, link targets and security
  notifications (#149, #224, #227).
- Risk-tolerance profile and consented date of birth stored as an age band (#326).
- PDF and CSV account statements with a download screen (#302).

#### Payments
- Bill payments: billers, validation and service, with fraud scoring and a daily limit shared with transfers (#233);
  a fraud-flagged bill payment is held for analyst review like a transfer (#258).
- Payee edit with re-verification, payee and bill-payment screens (#301).
- Scheduled and recurring transfers (#296) with a screen for them (#306); staff can clear a fraud block on a standing
  order (#363); a same-day retry is audited once per state change (#387).
- Transfer review and confirm with a source-account choice and a step-up modal; escalation after three wrong codes
  (#266).

#### Loans and wealth
- Loan catalogue, exact EMI schedule and explainable eligibility (#236); eligibility counts as income only credits that
  recur in 2 of the last 3 months (ADR-0043, #404); loan application, disbursal through the
  ledger, repayment schedule and loans screens (#309).
- Wealth catalogue, fixed-deposit booking and closure through the ledger, portfolio page (#327), and automatic payout
  of fixed deposits at maturity (#341).
- Insurance catalogue API and product comparison page (#337).
- Product recommendation engine (#204), held products as a recommendation input (#342) and a recommendations carousel
  on the accounts page (#328, #339).

#### Analytics and insights
- Spending trends and category aggregation (#156), financial health score (#158, #170), customer financial analytics
  screens (#221).
- Balance forecast with prediction intervals (#239) and an end-of-month projection chart (#318).
- Nightly analytics job and weekly segmentation schedule (#193).
- Relationship-manager insights API and dashboard (#223, #277), scoped to the customers assigned to each RM (#347),
  and ordered for an RM by the rule-based product score (ADR-0041, #391 via #397).
- Clickstream batch ingestion into partitioned storage (#222) and web instrumentation with route templates, bounded
  batches and an allow-listed event catalogue (#333).
- Compliance exceptions and behavioural analytics dashboards (#325).

#### AI assistant
- Read-only, approved tool registry and an Ollama router with a rules fallback (#243, #250).
- Input sanitiser, output filter, streamed replies (SSE), conversation history and an adversarial test suite (#244).
- Chat drawer in the web app with streamed replies and tool badges (#245).
- Unusual-expenses tool and loan suggestion without an amount (#331); FAQ, product discovery and investment suggestion
  tools (#335).
- Optional hosted Groq router for unclear questions, falling back to Ollama and then rules (ADR-0037, #368).

#### Staff, compliance and administration
- Audit log verification screen (#211).
- Staff fraud queue and alert detail with plain-language reasons and SHAP bars (#265, #299, #311), an analyst
  telemetry endpoint (#260) and a security monitoring dashboard (#278).
- Interactive fraud demonstration lab (#273).
- Administration screens and allow-listed runtime configuration (#346).

#### Web app
- Customer dashboard shell (#150), design tokens, UI primitives and shared error handling (ADR-0016, #200), and a
  role-aware responsive shell (#202).
- Registration and MFA enrolment screens (#300).
- Layout fixes for phone and wide screens (#228, #310, #353, #360, #386 via #397).

#### Platform and operations
- Cross-platform developer tooling for Ubuntu and native Windows (`make`, `dev.ps1`, `dev.cmd`) (#10, #25, #26, #132,
  #133, #136).
- Synthetic data generator with deterministic personas and a fast COPY load (#36, #138).
- MLflow tracking server with a local fallback (#36).
- TLS terminated in nginx and required to PostgreSQL (NFR-02, #280).
- k6 load suite, page-load budgets and hot-path indexes (#288); the API runs as two replicas, which the Performance
  workflow measures (ADR-0045, #419); provisioned Grafana dashboards, alert rules,
  availability probes and error tracking (#289); OpenTelemetry tracing (#205, #210).
- Release-tag verification and a health-gated staging deploy with rollback (#155); linux/amd64 and linux/arm64
  images (#432) and a staging guide for an Oracle Cloud Always Free Arm VM with a smoke test and availability probe
  (#433).
- Optional OTLP export to a local collector, with Celery and Redis tracing (ADR-0050, #430).
- Encrypted backups and a verified restore drill (#293).
- Scheduled Schemathesis API fuzzing (#154, #159, #168), Playwright critical-journey suite (#319, #340, #351, #359),
  mutation testing of the ledger and payments at 96.7% (#297).
- Faster health checks and ledger indexes (#137, #174).
- k6 load test for 500 clickstream events/s through `POST /api/v1/events/batch` (`make load-events`, TASK-055,
  #415).
- Outbox relay with retries and exponential backoff, audited dead letters, a daily purge and exactly-once delivery
  (ADR-0040, #393, TASK-026); enum CHECK constraints, append-only `security_events` and FK indexes (#393, #315).
- Celery worker counters and histograms exported through Redis and the API `/metrics` (ADR-0039, #380).
- nginx re-resolves the API container, so recreating the API alone no longer gives 502 (#411).

### Security
- MFA (TOTP) for staff, Argon2id passwords, JWT algorithm allow-list, refresh-token rotation with reuse detection,
  lockout, rate limits, CSRF guard and object-level authorization on every customer route (#36).
- Access tokens of disabled users and revoked sessions are rejected at once (ADR-0021, #281).
- `Idempotency-Key` required on step-up confirmation and the analyst decision (#364); unanswered step-ups expire and
  the idempotency key TTL is enforced (ADR-0036, #369).
- Free-text request fields reject control characters and unencodable text (#162, #169); notification offset and
  audit-log query parameters are bounded (#160).
- Customer PII keys (full name, date of birth, phone) are redacted from logs (#148); demo MFA secrets and passwords
  stay out of files and logs (#124).
- Pre-ship security audit, Spectre isolation headers and a nightly OWASP ZAP baseline (#290).
- PII field-encryption policy: email, name and date of birth are not field-encrypted, with the baseline verified
  (ADR-0042, #402).
- CI hardening: SHA-pinned third-party actions, PR-title check, admin-only merges (#117, #123, #125).
- `minimatch` forced to 11 under ESLint and typescript-eslint in the web build tooling (#434, #232).
- Dependency advisories fixed: dnspython CVE-2026-114605 (#141), pyasn1 (#207), and Dependabot updates (#31, #34,
  #229, #234, #241, #269).

### ML and fraud
- Fraud features with offline/online parity and a weighted rules registry (#247).
- LightGBM fraud classifier and Isolation Forest, evaluated on time-based splits (#248).
- Hybrid fraud combiner with calibrated weights, a rules floor, fail-safe to rules-only and SHAP reason codes (#249),
  served on transfers and bills behind a flag (#252), and on for local and demo after retraining on legitimate
  first-time behaviour (#257).
- Rules: login failures, new device or network and Redis sliding-window velocity counters (#255); new country from
  DB-IP Lite GeoIP (ADR-0014, #196); impossible travel from DB-IP City Lite (ADR-0034, #365); mule pass-through
  (ADR-0035, #367).
- Fraud behaviour-regression suite, velocity floor and an evaluation report (#256).
- Labelled fraud scenarios F1-F6 in the data generator (#235), plus F7 structuring and F8 dormant-account scenarios
  (#261).
- MLflow Model Registry champion/challenger promotion (#268).
- PSI drift monitor for amount and velocity (#336), evaluated hourly with a Grafana panel and alerts (ADR-0038, #376).
- Customer segmentation with K-Means and five personas (#161, #173, #192).
- Circular transfer / mule ring detection spike with NetworkX (#267).
- Training code kept off the request path, with an allow-listed artifact loader (#167, #354, #356).
- Data generator v2.3 (churned customers stop transferring) and a retrained `hybrid-lgbm-iforest-2` with an Optuna
  study and a SHAP summary (#392, #405). It was never promoted: the champion gate refused it on latency on a loaded
  machine.
- `hybrid-lgbm-iforest-3`: isotonic calibration fitted on out-of-time folds and a less regularised stack, PR-AUC
  0.945 / 0.887 and recall at REVIEW 0.923 / 0.894; it passed the champion gate (p95 20.2 ms) (ADR-0047, #421).
- Cheaper scoring: the Isolation Forest is scored without scikit-learn's per-call overhead, and LightGBM and TreeSHAP
  share one float64 matrix on one thread, with identical decisions; the share of transfers scored by rules only at a
  50 ms budget fell from 47.5% to 0.05% under load (#417, #422).
- Rules STRUCTURING (F7) and DORMANT_REACTIVATION (F8), `baseline-rules-0.6` (ADR-0046, #424), and an hourly
  deposit-side structuring monitor that opens REVIEW analyst alerts (`STRUCTURED_DEPOSITS`, ADR-0052, #436).
- Fraud feature contract 2 with `near_threshold_72h` and `days_since_last_activity`, and generator v2.4 with
  legitimate inbound transfers; the retrained `hybrid-lgbm-iforest-4` failed the champion gate on PR-AUC, so `-3`
  stays the champion; each model is served with its own columns (ADR-0051, #437).
- Product propensity model trained on a documented simulation of uptake; it failed its pre-registered gate, so
  `rules-v1` is still served (ADR-0049, #427).

### Fixes
- Payments and transfers: a fraud block on a standing order's final run completes it without a pause reason (#313).
- Loans: no instalment above the EMI when interest rounds a paisa over it (#242).
- Auth: a password change whose session was revoked in flight is refused with 401 (#423, closes #403).
- Ledger: the never-used FEE transaction type is removed (migration 0029, #431).
- Forecast: under 28 days of history the balance forecast uses a naive model, not weekly seasonal naive, so a
  one-off debit is not projected to recur (#425, #384).
- Clock: outbox due times and fraud event times use the app clock that reads them, which fixes failures on hosts
  whose database clock drifts (Docker Desktop on Windows); the assistant benchmark guards `os.getloadavg` (#426, #371).
- Analytics: re-materialise the posting month of reversed transfers (#171); exclude transfers between a customer's
  own accounts from segmentation features (#192); month-end analytics tests (#207); audit follow-ups with one-pass
  segmentation features, exact shares and burn rate, and documented 404s (#394 via #397); the recommendation context
  reads the health window once, 14 to 10 SQL statements (#395), then builds in 6 (#416).
- Web and API: a refresh without a cookie returns 204 and a forecast for a too-new account returns
  `INSUFFICIENT_HISTORY`, so expected states log no console errors (ADR-0044, #406).
- Fraud: a missing or unreadable GeoIP file disables NEW_COUNTRY instead of failing payments (#370); blank GeoIP
  paths mean no database, and reason times are in UTC (#372).
- ML: the training test uses its temporary MLflow store even when a local server runs (#279); hybrid latency tests
  are deterministic, the model budget may be set up to 100 ms, and the latency benchmark runs outside the suite (#379).
- Web: list an account opened while the account list is first loading (#353); payee row actions fit at phone width
  (#310); readable relative times in fraud reasons (#311); fixed-deposit recommendation opens the booking dialog
  (#339).
- Windows: runner defects (#132), backup passphrase privacy check (#334), unit tests on native Windows (#352), and
  `deploy.sh` and the backup pipeline test under Git Bash (#375); test runs copy only connection keys from `.env`
  and the benchmark load average is guarded on Windows (#409).
- Demo: the seed gives the demo customers a back-dated salary, and Alice a spending history, through guarded ledger
  postings that are not fraud-scored (ADR-0048, #418).
- Demo: Alice has an authenticator, the fraud lab uses its own customer and reuses sessions, a fresh demo payee, and
  the forecast starts at the current balance (#388).
- Tests: the backup pipeline test no longer races a closed pipe (#401); SHAP, regex, statement and travel tests check
  behaviour and cost instead of wall-clock time, and Redis-down tests fail fast (#412); five auth edge-case tests
  raise auth branch coverage to 95.83% (#410).
- CI: perf and DAST workflows wait on readiness (#295); deterministic tests for the fraud queue clock, device header
  and fraud stats window (#272, #307, #348).

### Docs
- Project blueprint, setup guide for Ubuntu and Windows, contributor workflow for AI-assisted development (#25, #120,
  #127).
- Architecture: high-level design with C4 views (#262), low-level design and data model (#312, #322), ER diagram
  drift-checked in CI (#259).
- Requirements specification and user stories traced to tasks and tests (#176, #263).
- Security: threat model v1.0 and gap issues (#140, #147), AI assistant security architecture (#246), FIDO2/WebAuthn
  evaluation (#172).
- ML: model cards, governance pack and AI governance alignment (#321, #344); why recommendations stay rules-based
  (#345).
- Operations: deployment and operations runbook (#152), private-repo readiness checklist (#349).
- Testing: local verification run (#191), test strategy and verification report (#373).
- Demo: live demo script with verified amounts and a fallback plan (#377).
- User manual with customer, staff and compliance guides (#374).
- ADRs 0001-0052 (0026 is not used) and the team execution plan with batch status updates (#118, #119, #121, #122, #126, #139, #142,
  #146, #151, #153, #157, #163-#166, #190, #195, #197, #199, #203, #208, #225, #226, #230, #240, #270, #291, #294,
  #298, #308, #320, #338, #355, #357, #361, #408).
