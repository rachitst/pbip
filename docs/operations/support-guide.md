# PBIP support guide

**Audience:** first-line (L1) and second-line (L2) support for a running PBIP stack: the local demo stack
(`docker-compose.yml`, https://localhost:8443) or the staging host (`infra/deploy/docker-compose.prod.yml`, project
`pbip-prod`). **Scope:** how to triage a report, the common issues with their diagnosis and fix, who to escalate to, and
where the logs, metrics and dashboards are. Every procedure here points at the [runbook](runbook.md), which stays the
authority for deployments, secret rotation, backups and incident playbooks.

Conventions: `<placeholder>` values are yours. Commands for the staging host run from `~/pbip` and use
`docker compose -f docker-compose.prod.yml`; on the local stack drop the `-f` option. Never paste secrets, tokens,
TOTP codes, full account numbers or customer data into tickets or chat; refer to a customer by user id or by the
`request_id` of the failing request.

---

## 1. Support levels

| Level | Who | Does | Does not |
|---|---|---|---|
| **L1** | Support desk; staff with the role the request needs (fraud analyst, compliance officer, relationship manager, admin) | Takes the report, collects the facts in section 2, answers known questions from the [user manual](../user-manual/README.md), uses the staff screens (`/staff/fraud`, `/staff/monitoring`, `/staff/compliance`, `/staff/admin`) | Runs commands on a host, reads raw logs, changes configuration |
| **L2** | Platform engineer on call | Reads logs, metrics and dashboards, runs the diagnosis commands below, applies a documented fix or a runbook procedure | Edits code or data on a host; every code fix goes through a pull request and a tagged release |
| **L3** | Owning engineer for the area (section 5) | Root cause, hotfix PR, ADR if the design changes | |

Two rules hold at every level: act as the smallest role that can do the step, and every containment action leaves a
record (`security_events`, `audit_logs` or the incident issue). Money is never moved by hand: the only correction is a
compensating transaction through the ledger owner (AGENTS.md section 4).

## 2. Triage checklist (L1)

Collect these before anything else:

1. **Who and where:** the user's role (customer or which staff role) and the environment (local demo or staging).
2. **What and when:** the page or action, the time (with time zone), and what the user expected.
3. **The `request_id`.** Every API error body carries one (RFC 7807 problem+json), and every response has the
   `X-Request-ID` header. It is the key to the logs; ask for it or for a screenshot of the error.
4. **The HTTP status, if shown:** 401 (not signed in or session ended), 403 (role or MFA missing, or a declined
   transfer), 404 (not found, which also covers "not yours"), 409 (state conflict), 422 (validation or a business rule),
   423 (account locked), 429 (rate limited), 5xx (server).
5. **Scope:** one user, several, or everyone. Everyone means L2 now (section 4, "Is the stack up?").

Then match the report to section 3. If it matches nothing, or the fix needs a host, hand it to L2 with the facts above.

## 3. Common issues

### 3.1 Sign-in and MFA

| Symptom | Diagnosis | Fix |
|---|---|---|
| "Wrong email or password" (401) | The API never says which one was wrong. L2: `security_events` for the user show `LOGIN_FAILURE` with reason `bad_password` or `unknown_user` | The user retries with the right credentials; a forgotten password has no self-service reset in PBIP (staff: an admin creates a replacement user, runbook section 5.2 step 6) |
| "Account temporarily locked" (423) | After `LOGIN_MAX_FAILURES` (5) failed passwords or codes, the account locks for `LOGIN_LOCK_BASE_SECONDS` (60 s), doubling with every further failure. Events `ACCOUNT_LOCKED`, `LOGIN_BLOCKED_LOCKED`; the lockout also appears on the compliance exceptions page (`ACCOUNT_LOCKOUT`) | Wait for the lock to expire, or an admin checks `GET /api/v1/admin/users/{user_id}/lockout` and clears it with `POST /api/v1/admin/users/{user_id}/unlock` (screen `/staff/admin`; audited, and it also clears the login rate-limit counters of the user's recent IPs) |
| "Invalid verification code" on the MFA step | The TOTP code is wrong, was already used (replay protection), or the phone clock is off; the challenge token also expires after `MFA_CHALLENGE_TTL_MINUTES` | Check the phone's time is set automatically, wait for the next code, or use a recovery code; repeated wrong codes count towards the lockout |
| Staff user signed in but every staff page says access denied (403) | Staff routes need the role **and** a session that completed TOTP (`amr` contains `otp`). A password-only session is refused | Enrol MFA (staff "Sign-in security" page), sign out and sign in again with the code |
| Signed out unexpectedly | Access tokens last `JWT_ACCESS_TTL_MINUTES` (15) and are renewed from the refresh cookie. A password change, a disabled user, a reused refresh token (`TOKEN_REUSE_DETECTED`) or a `JWT_SECRET` rotation ends sessions (ADR-0021) | Sign in again. If `TOKEN_REUSE_DETECTED` appears for the user, treat it as a possible stolen session: escalate to the security owner (runbook section 5.2) |
| Session not restored after a reload on a new host name | The refresh and logout endpoints reject an `Origin` that is not in `CORS_ORIGINS` (403) | L2: add the exact `https://<host>` origin to `CORS_ORIGINS` in `.env` and recreate `api` (runbook section 2.3) |

### 3.2 Too many requests (429)

Rate limits are per client IP, counted in Redis, and the response carries `Retry-After` (seconds).

| Endpoint group | Limit (setting, default) |
|---|---|
| Sign-in and MFA code (`/auth/login`, `/auth/mfa/verify`) | `RATE_LIMIT_LOGIN_PER_MINUTE`, 10 per minute |
| Registration | `RATE_LIMIT_REGISTER_PER_HOUR`, 10 per hour |
| Password change | `RATE_LIMIT_PASSWORD_CHANGE_PER_MINUTE`, 5 per minute |
| Assistant questions | `RATE_LIMIT_ASSISTANT_PER_MINUTE`, 10 per minute; Groq routing additionally `RATE_LIMIT_GROQ_PER_MINUTE`, 5 |
| Clickstream batches (`/events/batch`) | `RATE_LIMIT_EVENTS_PER_MINUTE`, 120 per minute |
| Phone number change | `RATE_LIMIT_PHONE_CHANGE_PER_HOUR`, 10 per hour |

**Diagnosis.** Many users behind one NAT or proxy share an IP, so a classroom or office can hit the login limit
together. Check whether the 429s come from one IP or many: `http_requests_total{status="429"}` by route on the
*API latency and errors* dashboard, or the API logs. A burst of 429s on `/auth/login` together with `LoginFailureSpike`
is a credential-stuffing signal, not a support issue: escalate to the security owner.

**Fix.** Wait for the window (`Retry-After`). An admin unlock (section 3.1) clears the login counters of that user's
recent IPs. Raising a limit is a configuration change in `.env` (L2, recorded on the operations issue), never a code
edit on the host. If Redis is unavailable, rate limiting cannot count; check readiness (section 4).

### 3.3 A transfer or bill payment is "being reviewed" (held)

**What the customer sees.** "Your transfer is being reviewed for your protection" (202): the fraud engine scored the
payment `REVIEW`, or the customer entered three wrong step-up codes. The money is **held** (not available, not
sent) and an alert is in the analyst queue. The customer is never told the score or the reasons, and support must not
tell them either.

**Diagnosis (fraud analyst, MFA session).** Find the alert in `/staff/fraud` (or `GET /api/v1/fraud/alerts`); the
detail (`GET /api/v1/fraud/alerts/{alert_id}`) shows the score, the reasons with their source (RULE, MODEL, ANOMALY)
and the model version. The [fraud alert flow](../diagrams/activity.md#3-fraud-alert-management-uc-10) shows every
outcome.

**Fix.** The analyst decides `RELEASE` (posts the payment, customer notified) or `REJECT` (hold released, payment
declined, customer notified) with a note. Support never releases a payment on a customer's word alone. Related cases:

| Case | What happened | What to do |
|---|---|---|
| "Additional verification is required" and nothing happens | The payment waits for a TOTP code (`PENDING_STEP_UP`). It expires after `STEP_UP_EXPIRY_MINUTES` (15) and is declined with `STEP_UP_EXPIRED`; no money was held | Ask the customer to send the transfer again and enter the code in time |
| "We could not process this transfer" (403) | Declined by the fraud engine (`FRAUD_BLOCK`) or by an analyst; an alert exists | Fraud analyst reviews; a declined transfer is never "re-run", the customer sends a new one if appropriate |
| A standing order stopped with a fraud block | The scheduled run was declined, so the order is `PAUSED` with `FRAUD_BLOCK` and the customer cannot resume it | After review, a fraud analyst clears it (`POST /api/v1/fraud/scheduled-transfers/{scheduled_transfer_id}/clear`); the customer then resumes it on `/scheduled` |
| A standing order paused "action required" | `CUSTOMER_ACTION_REQUIRED`: the account is no longer active, or the payee is gone or unverified | The customer fixes the cause (verify the payee, use an active account) and resumes it |
| "Daily limit exceeded" (422) | The customer reached `DAILY_TRANSFER_LIMIT_MINOR` (default ₹5,00,000.00 per UTC day, transfers and bills together); single payments are capped by `MAX_TRANSFER_MINOR` (₹2,00,000.00) | Explain the limit; a scheduled payment retries on later ticks the same day and is reported missed after that |

### 3.4 Statements

| Symptom | Diagnosis | Fix |
|---|---|---|
| 422 "the statement cannot end in the future" or "start date must be on or before the end date" | Dates are IST bank days; the end date may be today at the latest | Choose valid dates |
| 422 "a statement can cover at most 366 days" or "too many transactions in this period" | The period limits protect the PDF renderer | Choose a shorter period, or download two statements |
| 404 on an account | The account is not the signed-in customer's (ownership is checked on every request; "not yours" and "does not exist" look the same) | The customer uses their own account; never look up someone else's |
| The customer received a "statement downloaded" notification they did not expect | Every download is audited (`STATEMENT_DOWNLOADED`) and notified, so customers learn when their history leaves the bank | Treat as a possible account compromise: check `security_events` for the user and escalate to the security owner if the sign-ins are unfamiliar |

### 3.5 The assistant answers slowly or only with simple answers

**How it works.** Clear questions go straight to a tool through the keyword rules (no model call, answers in well
under a second). Only unclear questions go to a model: Ollama on the host by default, or Groq first when
`LLM_PROVIDER=groq`. If the model is off, slow or wrong, the rules answer instead, so the assistant keeps working
without any model ([assistant sequence](../diagrams/sequence.md#3-assistant-question-to-answer)).

| Symptom | Diagnosis (L2) | Fix |
|---|---|---|
| Unclear questions get the help text or a generic answer | The model route failed and the rules answered. API logs show `assistant_llm_fallback` with an `outcome` (`timeout`, `rate_limited`, `error`, `invalid`, `budget`, `cooldown`); the audit trail has one `assistant.llm_call` row per provider tried | Local stack: start Ollama on the host (`ollama serve`) and pull the model in `OLLAMA_MODEL` (default `qwen2.5:1.5b-instruct`); raise `OLLAMA_TIMEOUT_SECONDS` only on a slow demo machine. Groq: check `GROQ_API_KEY` and `GROQ_MODEL` are set (the API refuses to boot with `LLM_PROVIDER=groq` without them) and see runbook section 3.3 |
| Every answer is slow | The model is being loaded (first question after a restart) or the host is overloaded | `OLLAMA_KEEP_ALIVE` (default 24h) keeps the model loaded; check host load |
| The assistant refuses a question | The guard refuses money movement, other people's data, SQL, privilege requests and instruction overrides by design (`assistant.refused` in the audit trail) | Expected behaviour; point the customer to the right page |
| 429 | `RATE_LIMIT_ASSISTANT_PER_MINUTE` (section 3.2) | Wait a minute |

Staging sets no `OLLAMA_*` variables, so on staging every answer comes from the rules route; that is expected.

### 3.6 Fraud alert backlog

**Signal.** The *Security monitoring* page (`/staff/monitoring`, `GET /api/v1/fraud/monitoring`) shows open alerts by
severity with the age of the oldest; the *Fraud decisions* Grafana dashboard shows the REVIEW and BLOCK rate;
`FraudBlockRateHigh` fires on a high block share.

**Diagnosis.** Is the queue growing because there are more alerts (a fraud spike or a misfiring rule), or because
nobody is deciding them? Read the dominant reason codes on the newest alerts. Customers with held money wait for
every alert in the queue, so the oldest `HIGH` alerts come first.

**Fix.** Assign more analysts (alerts are not pre-assigned; any analyst can decide any open alert). If one rule fires
on legitimate traffic, or many customers are affected, follow the **fraud spike** playbook (runbook section 5.1): it
covers classification, containment (disabling users, lowering `DAILY_TRANSFER_LIMIT_MINOR`) and the rule hotfix path.
If payments post without `fraud_events` rows, stop money movement as the playbook says.

### 3.7 Background jobs (Celery beat)

The beat schedule is in `apps/api/src/pbip_api/worker/celery_app.py`; the [component diagram](../diagrams/component.md)
lists every job and its time.

| Symptom | Likely job | Diagnosis (L2) | Fix |
|---|---|---|---|
| No new notifications in the bell | `relay-outbox` (every 10 s) | `docker compose ps worker beat`; worker logs; `CeleryNoWorker`; dead letters are audited `OUTBOX_EVENT_DEAD_LETTERED` and logged `outbox_event_dead_lettered` after `OUTBOX_MAX_ATTEMPTS` (6) | Start or recreate `worker` and `beat` (runbook section 2.3). Pending events are kept and delivered exactly once when the relay runs again |
| A standing order did not run | `execute-scheduled-transfers` (every 60 s) | `scheduled_transfers_total{outcome}` on the *Celery jobs* dashboard; the order's `last_failure_reason`; audit rows `SCHEDULED_TRANSFER_EXECUTION_FAILED`, `SCHEDULED_TRANSFER_MISSED`, `SCHEDULED_TRANSFER_AUTO_PAUSED` | Funds or limit: retried the same day, then reported missed. Paused: section 3.3. Worker down: restart it; missed dates are recorded and skipped, never paid twice |
| A step-up transfer stays "awaiting verification" past 15 minutes | `expire-stale-step-ups` (every 60 s) | Worker running? | Restart the worker. A late code is refused anyway after the deadline, so no money can move |
| A matured fixed deposit was not paid out | `pay-out-matured-deposits` (hourly at :05) | `fd_maturity_payouts_total{outcome}`; audit `FIXED_DEPOSIT_PAYOUT_HELD` means the payout account is frozen or closed | Once the account is active again, the next hourly run pays out, exactly once |
| Insights or segments look a day old | `materialize-monthly-spending` (02:00), `segment-customers-weekly` (Sunday 03:00) | Worker logs for the run at that time | Expected between runs; if a run failed, restart the worker; the next run recomputes |
| A customer's behavioural profile is missing or a day old (RM flyout, `GET /api/v1/analytics/behaviour-profile/me`) | `build-behaviour-profiles-daily` (03:40 UTC) | Worker logs for the 03:40 run; the customer's analytics consent (`GET /api/v1/customers/me/analytics-consent`, or the Profile page) | Expected without consent: no profile is kept, and withdrawing consent erases it. With consent, the next 03:40 run builds it (idempotent, advisory-locked); restart the worker if the run did not happen |
| Clickstream events pile up in `clickstream_events_default` | `ensure-clickstream-partitions` (01:30) creates the monthly partitions months ahead | Worker logs for the 01:30 run | Nothing is lost: an event with no monthly partition lands in the default partition. Restart the worker so the job runs; it is idempotent |
| Drift alerts | `evaluate-feature-drift` (hourly at :20) | `FeatureDriftWarning`, `FeatureDriftCritical`; gauge `pbip_feature_psi` | Not a support fix: escalate to the ML owner |

**Beat must run as exactly one container.** Two beats publish every job twice. The jobs themselves are idempotent
(advisory locks, per-occurrence idempotency keys, row locks), so a duplicate does no harm to money, but fix it:
`docker compose ps beat` must show one container.

### 3.8 Backups

| Symptom | Diagnosis | Fix |
|---|---|---|
| `scripts/backup.py` exit code 1 | The backup failed and kept nothing | Read the error (database not reachable on loopback, `gpg` missing, passphrase file readable by others); fix and run again (runbook section 4.1) |
| Exit code 2 | The backup was written but the **source** failed an integrity check: broken audit chain, an account whose balance differs from its entries, or an unbalanced transaction | Keep the file as evidence and open an incident with the security owner and the ledger owner. Do not "repair" data by hand |
| Restore drill does not report `PASS` | `scripts/restore.py` restores into a scratch database and compares the manifest | Follow runbook section 4.2; a failing drill means the backup cannot be trusted |
| No recent backup off the host | Scheduling and off-host storage are set up per host (Oracle guide section 15) | Run a backup now and copy it off-host; record it in the backup log |

## 4. Where to look

| What | Where | Notes |
|---|---|---|
| Is the stack up? | `GET /api/health/live` and `GET /api/health/ready` (checks PostgreSQL and Redis); `python3 scripts/smoke_test.py https://<host>`; `docker compose ps` | Staging also has the availability probe log and `python3 scripts/uptime_report.py` (Oracle guide section 17) |
| Logs | `docker compose logs --since 1h api` (also `worker`, `beat`, `web`, `db`); with the monitoring profile, Grafana Explore on Loki: `{service="api"} \| json \| request_id="<id>"` | JSON, redacted: no passwords, tokens, emails or account numbers. Unhandled errors log one `unhandled_exception` event with `request_id`, route template and `error_type` |
| Metrics | API `/metrics` on the internal network only (nginx answers 404); Prometheus at http://localhost:9090 with the monitoring profile | Worker counters are exported by the API with `source="worker"` (ADR-0039) |
| Dashboards | Grafana http://localhost:3000, folder "PBIP": *API latency and errors*, *Fraud decisions*, *Celery jobs and PostgreSQL*, *Availability*, *Overview* | Local stack with `make up-all`; the staging compose has no monitoring profile (Oracle guide section 16) |
| Alerts | http://localhost:9090/alerts; rules in `infra/prometheus/alerts.yml` | No notification channel is configured; alerts are seen in Prometheus and Grafana only (runbook section 1.5) |
| Security events | Security monitoring page `/staff/monitoring` (analyst or admin); `security_events` table for L2 | Lockouts, failed-sign-in bursts, high-risk customers, open alerts |
| Audit trail | `/staff/audit-log` and `/staff/compliance` (compliance officer or admin); `GET /api/v1/compliance/audit-logs/verify` recomputes the hash chain | A failed verification is an incident (`AUDIT_CHAIN_FAILURE` on the exceptions page) |
| Traces (optional) | `otel` profile; spans are printed by the collector | Off by default (ADR-0050) |

## 5. Escalation matrix

| Situation | First responder | Escalate to | Within | Reference |
|---|---|---|---|---|
| Stack down: readiness failing, `HealthProbeFailing`, `AvailabilityBelow99`, 5xx for everyone | L2 platform on call | Platform owner | 15 minutes | Runbook sections 1.3, 2.2 (roll back) |
| Suspected stolen session or credential, `TOKEN_REUSE_DETECTED`, admin actions nobody recognises | L1 takes the report, L2 preserves evidence | Security owner | Immediately | Runbook section 5.2 |
| Fraud spike, alert backlog growing faster than it is cleared, `FraudBlockRateHigh` | Fraud analyst | Fraud analyst lead, then platform owner for containment | 15 minutes | Runbook section 5.1, section 3.6 above |
| Payments posting without fraud scoring, or `FraudScoringSlow` | L2 | Platform owner and ML owner | Immediately: stop money movement first | Runbook section 5.1 step 7 |
| Money in the wrong place, a balance that does not match its entries, backup exit code 2 | L2 | Ledger and payments owner (CODEOWNERS) and security owner | Immediately | Runbook section 4; AGENTS.md section 4 |
| Audit chain verification fails | Compliance officer | Security owner | Immediately | Runbook section 5.2 step 5, section 4 |
| Login problems for one user | L1 | Admin (unlock) | Same day | Section 3.1 |
| Assistant degraded to rules | L2 | Assistant owner | Next working day | Section 3.5; runbook section 3.3 |
| Background job not running | L2 | Platform owner | 1 hour (money jobs), next working day (analytics jobs) | Section 3.7 |
| Feature drift or model alerts | L2 | ML owner | Next working day | ADR-0038 |
| TLS certificate close to expiry | L2 | Platform owner | 7 days before expiry | Runbook section 1.4 |

Owners by area follow `.github/CODEOWNERS` and the module owners in the repository; the security owner and the
platform owner sign off any customer communication about an incident.

## 6. Related documents

* [Runbook](runbook.md): deployments, rollback, secret rotation, backup and restore, incident playbooks.
* [Deployment guide](deployment-guide.md): the release path and the model files.
* [Oracle Cloud Arm staging guide](deploy-oracle-cloud-arm.md): the staging host.
* [User manual](../user-manual/README.md): what customers and staff see.
* [Diagrams](../diagrams/README.md): the flows referenced above.
* [Threat model](../security/threat-model.md): the controls behind the security answers.
