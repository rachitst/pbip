# Changelog

All notable changes to PBIP are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/). Numbers in brackets are the pull requests that made
each change. The plain-language summaries are in [`docs/release/`](docs/release/).

## [1.1.4] - 2026-11-08

### Fixed

- **Rolling deploy, a stale replica address:** on the v1.1.3 deploy one request hung while an nginx worker still
  held a retired api replica's address. nginx now reloads straight after each old replica stops, and gives up on
  an api connect after 1 s and tries the next replica (`proxy_connect_timeout 1s`, `proxy_next_upstream error
  timeout`; ADR-0062 update, #505).

## [1.1.3] - 2026-11-08

Deploys that keep the API answering. The summary is in [`docs/release/v1.1.3.md`](docs/release/v1.1.3.md).

### Fixed

- **Deploy cutover:** with api replicas running, `deploy.sh` (deploy and rollback) now starts the new replicas beside
  the old ones, waits for their health checks, and only then stops the old ones one at a time with an nginx reload
  after each. New replicas that never turn healthy are removed and the running release keeps serving. The v1.1.2
  deploy had been measured at 12-15 s without a ready answer. `web` (nginx, ports 80/443) is still recreated
  (ADR-0062, #505, #507).

### Added

- **Docs:** runbook section 2.5 (SSH ingress and `DEPLOY_ENABLED` until the final presentation) and section 4.5
  (Oracle idle reclamation: accepted risk and recovery from the off-host backups); the teammates' peer reviews of
  the v1.1.0 requests recorded; #101 (TASK-078) and #198 closed by owner decision.

## [1.1.2] - 2026-11-08

Staging over HTTPS, the trained models on the staging host and the assistant's Groq budget. The summary is in
[`docs/release/v1.1.2.md`](docs/release/v1.1.2.md).

### Changed

- **Assistant:** the per-customer Groq budget is 10 questions a minute (was 5), and a customer over budget gets the
  keyword rules at once instead of waiting for Ollama, which takes longer than its 4 s timeout on a 2-CPU host;
  a 429 cooldown, a 5xx or an invalid answer still fall back to Ollama (ADR-0037 addendum, #502).

### Added

- **Trained models on the staging host (opt-in):** `ML_MODELS=enabled` in the host `.env` makes `deploy.sh` add
  `docker-compose.prod.models.yml`, which serves the hybrid fraud scorer (API and worker) and the recommendation
  ranker (API) from read-only mounts of operator-copied model files; the deploy stops if the files are missing
  (ADR-0061, #503).
- **Docs:** staging over HTTPS (Let's Encrypt), the TASK-102 page-load and login budgets measured on staging, the
  k6 suite with the load generator off the host, the at-rest evidence that makes ADR-0042 final (#399), and the
  traceability and threat-model updates.

## [1.1.1] - 2026-11-08

The first release deployed to the staging host (Oracle Cloud Always Free Arm VM, `https://pbip.duckdns.org`), with the
fixes and operations work that host needed. The summary is in [`docs/release/v1.1.1.md`](docs/release/v1.1.1.md).

### Fixed

- **Production deploys:** the Celery worker and beat no longer inherit the API image's HTTP health check, which made
  every `deploy.sh` release fail its health gate (#494).
- **Database pool starvation under load (TASK-079):** each API process admits no more requests than its pool has
  connections, and every request uses at most one connection, so 200 and 500 virtual users run with 0 errors on the
  staging host instead of collapsing into 30 s pool timeouts; a request that waits past the pool timeout gets a 503
  `service_busy` with `Retry-After` (ADR-0060, #499).

### Changed

- **Assistant latency:** clear "overview of my finances" questions are answered by the rules, and the Groq routing
  call has a 2.5 s wall-clock deadline after which the rules answer at once; first token for open questions p50
  364 ms (was 688 ms), p95 729 ms (was 1,737 ms), worst case 850 ms (was 8,093 ms) (ADR-0037 addendum, #497).

### Added

- **Monitoring for the production stack:** Prometheus, Grafana, Loki, Alloy and the exporters as their own compose
  project beside `pbip-prod`, loopback-only, passwords as compose secrets, `deploy.sh monitoring` (ADR-0059, #498).
- **Off-host backups:** `scripts/offsite_backup.py` and `infra/deploy/backup-offsite.sh` copy the encrypted daily
  backup to OCI Object Storage through its S3-compatible API; a restore drill of the downloaded copy passed (#496).
- **Docs:** staging results ([performance 2026-11-08](docs/testing/performance-2026-11-08-oci.md)), the 2 OCPU /
  12 GB shape and a certbot `--deploy-hook` fix in the Oracle guide and runbook, traceability updated.

## [1.1.0] - 2026-11-05

The release that completes the use-case document's functional scope (sections 5 and 7) and its deliverables (sections
9 to 11). Tagged `v1.1.0` on `main` on 5 November 2026; the summary is in
[`docs/release/v1.1.0.md`](docs/release/v1.1.0.md).

### Added

- **UC-06 loan recommendations:** catalogue loans (personal, car, home) are recommended with an eligibility
  assessment from balances, income and spending, a suggested range and EMI, and reasons; `GET /api/v1/loans/offers`;
  eligibility v2 adds a spending rule and a balance rule (ADR-0056, #470).
- **UC-07 risk tolerance:** the RTQ-1 band is a suitability rule in `/recommendations` and shown in `/wealth/compare`
  (ADR-0056, #461, #483).
- **UC-11 behaviour analytics:** privacy-safe session duration, product interests, a per-customer behavioural profile
  (customer API, RM/admin view), recorded analytics consent with erasure on withdrawal, and staff charts for product
  affinity and engagement (ADR-0057, migration 0030, #463, #477).
- **Wealth:** simulated investing in funds (lump sum and monthly SIP), stocks and bonds through the ledger,
  idempotent and audited, with holdings, sell and gain/loss in the portfolio; a simulated insurance quote and
  application (no premium debited); every recommendation card has a working action (ADR-0058, migration 0031, #465,
  #483, #484, #487).
- **Staff dashboards:** a fraud monitoring dashboard charting `/fraud/stats` with accessible charts (ADR-0016, #464);
  dead-lettered notifications and failed scheduled transfers in compliance exception management (#466).
- **Assistant:** insurance in product discovery and an FAQ that covers every customer feature (#474, #482).
- **Tests:** E2E journeys for statements, bill payments and loans (TASK-077, #472).
- **Docs:** 25+ diagrams with SVG/PNG exports (#462, #478), use cases UC-01 to UC-13 as built with screenshots,
  traceability against final `main`, support and deployment guides, README landing page, docs index, review process
  and peer-review requests (#467, #468, #469, #476); a Markdown link check in CI (#469).
- **Demo:** a Playwright walkthrough recorder and editor (#480); the A-to-Z walkthrough and highlights videos are
  attached to the release.

### Fixed

- Load-sensitive tests: the ReDoS growth test times both sizes alternately; hang guards on long fuzz and model tests;
  the bundle tests share the trained model; the velocity concurrency test warms the fakeredis keyspace (a test-double
  race, not a production bug) (#475, #479, #485).

## [1.0.2] - 2026-11-04

A patch release on top of 1.0.1, tagged `v1.0.2` on `main` on 4 November 2026. The plain-language summary is in
[`docs/release/v1.0.2.md`](docs/release/v1.0.2.md).

### Changed

- **API:** `POST /api/v1/events/batch` accepts at most 25 events per batch (was 100); 26 or more get a 422
  (`too_long`) and nothing is stored. This keeps request p95 under TASK-055's 15 ms. An asynchronous insert was
  measured and rejected because it could not be both lossless and faster (ADR-0055, #458, #78). The web app
  already sends at most 25 events per batch, and the k6 ingestion scenario now uses batches of 25.

### Fixed

- `test_config.py` no longer reads a developer's `.env`, which could mask two production-safety cases (#457).

### Documentation

- TASK-093 is Done, with the v1.0.1 fallback video (#457). TASK-054 is Done: the owner accepted the first-request
  latency floor and kept the per-request token-revocation check uncached (ADR-0021, #343). TASK-055 is Done
  (ADR-0055).

## [1.0.1] - 2026-11-04

A patch release on top of 1.0.0, tagged `v1.0.1` on `main` on 4 November 2026. The plain-language summary is in
[`docs/release/v1.0.1.md`](docs/release/v1.0.1.md).

### Fixed

- DORMANT_REACTIVATION measured 181 dormant days as 180, and the staff reason text could say "became active again -1
  days ago", when the database clock ran ahead of the app clock (Docker Desktop). Transaction headers and account
  openings are now stamped by the app clock, and the day count is never negative (#447, #442).

### Added

- A byte-reproducible, SHA-256-pinned bundle of the served fraud model `hybrid-lgbm-iforest-3`, attached to the
  v1.0.0 release; `make fraud-model-release` installs it with its checksum, manifest and model version verified
  (#444, TASK-084). The SHAP summary plot is logged to the champion's MLflow run.
- Recommendation ranker trained on a behaviour-driven **simulation** of product uptake: the logistic regression
  `sim-propensity-v2` passed its pre-registered gate against rules-v1 on held-out simulated months and re-orders the
  eligible products when its model file is installed, with rules-v1 as the fallback and every response labelled as
  simulated (#445, ADR-0053, TASK-054).
- Playwright recorder for the fallback demo video, `demo/record/` (#443, TASK-093).
- Fuzzing reaches owned resources and staff routes, and the contract documents every status code the fuzzer meets
  (401, 403, 429, 400 and route-specific 404/409/423); `status_code_conformance` is on (#450, #175 M11).

### Changed

- The recommendation context and the balance-forecast fit are cached in process under a version re-read on every
  request, so a cached value is never older than its inputs and never crosses customers (ADR-0054, #451, #343).
- A clickstream batch is inserted with one fixed, compiled-once statement (#449, TASK-055); the Performance workflow
  records event ingestion latency.
- Wall-clock assertions in the structuring, COPY-load and mule-scoring tests are replaced by cost and query-shape
  assertions; the timing moved to benchmark scripts (#446, #454).

### Documentation

- User-manual RM insights screenshots re-shot (#439); final batch docs and v1.0.0 marked as tagged (#440); plan,
  traceability and these notes (the v1.0.1 docs update).

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
