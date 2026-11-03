# End-to-end tests: critical journeys (TASK-077)

Playwright drives a real Chromium against the full Docker Compose stack (nginx on `https://localhost:8443`
-> API -> PostgreSQL, Redis, Celery). The specs live in `apps/web/e2e/`; shared helpers are in
`apps/web/e2e/support.ts`. They sit at the top of the test pyramid (AGENTS.md section 10): few tests, each a
whole journey a customer or an analyst depends on.

## The journeys

| # | Journey | Spec and test |
|---|---|---|
| 1 | Register in the UI (#201), enrol an authenticator in Profile & security, sign out, sign back in with a code, receive an opening balance | `journey-onboarding.spec.ts`: "a customer registers, enrols an authenticator, signs back in with a code and is funded" |
| 2 | Send money through review to a receipt; the Idempotency-Key is a UUID and a replay with it returns the same transfer (`Idempotent-Replayed: true`); the entry is under Accounts -> Transactions | `journey-transfer.spec.ts`: "a transfer goes through review to a receipt, is idempotent and shows in the ledger" |
| 2b | Step-up: INR 25,000 to a new payee asks for the authenticator code, then posts | `journey-transfer.spec.ts`: "a large transfer to a new payee asks for the authenticator code, then posts" |
| 3 | A high-risk transfer is held; the analyst signs in with MFA, finds it in the queue (customer name, main reason) and releases it; the customer sees it posted | `journey-fraud-review.spec.ts`: "a held transfer is released by an analyst and the customer sees it posted" |
| 3b | Same, but the analyst rejects and the held money returns (no ledger entry) | `journey-fraud-review.spec.ts`: "a held transfer is rejected by an analyst and the money returns" |
| 4 | A standing order is scheduled on `/scheduled` (TASK-021) and listed as upcoming, also after a reload | `journey-scheduled.spec.ts`: "a scheduled transfer is created and listed as upcoming" |
| 5 | The assistant drawer opens, answers "What is my balance?" with the real balance, and closes | `journey-assistant.spec.ts`: "the assistant answers a balance question from the customer's own account" |
| 6 | BOLA: another customer's account, payee and transfer ids in a URL give 404 `not_found`, exactly like ids that do not exist; a deep link with them opens nothing | `journey-bola.spec.ts`: "another customer's ids in a URL give Not found, like ids that do not exist" |
| 7 | Administration (TASK-108): an ADMIN signs in with MFA, changes the standing-order cap with a reason and restores it, creates a staff user and clears its lockout; an RM gets no link, a restricted page and 403 `forbidden` from the admin API | `journey-admin.spec.ts` (#359) |
| 8 | Compare products (TASK-098) at 1280 and 360 px: the product checkboxes keep their size and the names get the row (a layout guard jsdom cannot give), then two products open the comparison table | `journey-compare.spec.ts` (#360) |

The older specs stay: `smoke.spec.ts`, `sprint1.spec.ts` (account opening, devices, password change),
`shell.spec.ts` (routing, roles, phone width) and `perf.spec.ts` (TASK-102 page and login budgets).

## Why they are deterministic

* **Own data per test.** Every test registers its own customers (`e2e-<label>-<time><uuid>@example.com`, a
  password from `crypto.randomUUID()`), so no test depends on another, on spec order or on leftover data.
  Workers can run the files in parallel.
* **Opening balance.** PBIP has no customer-facing deposit; money arrives as an opening balance. Journeys that
  need money fund their fresh customer with `scripts/e2e_fund.py`, which posts a ledger deposit through
  `ledger.deposit` (the same sanctioned path as `make seed`). It refuses staging/production, any address
  outside `example.com` (so it can never touch a demo or real customer) and amounts above INR 10,00,000. The
  API image ships no `scripts/`, so the helper pipes the file into the API container:
  `docker compose exec -T api python - <email> <amount_minor> < scripts/e2e_fund.py`.
* **Fraud decisions.** The engine is not stubbed or bypassed. Each money journey uses a fresh customer whose
  first transfer goes to a payee added seconds earlier, with no failed sign-ins and no history, so the inputs
  are the same on every run:

  | Input | Rules (`baseline-rules-0.4`, all CI has: no model files) | Trained hybrid (`hybrid-lgbm-iforest-1`, local) | Outcome |
  |---|---|---|---|
  | INR 100 to a new payee | NEW_PAYEE, 15 -> ALLOW | 0 | POSTED |
  | INR 25,000 to a new payee, customer with MFA | NEW_PAYEE_LARGE_AMOUNT, 45 -> STEP_UP | 30 (the rule's STEP_UP floor) | step-up, then POSTED |
  | INR 25,000 to a new payee, customer without MFA | 45 -> STEP_UP | 30 | no second factor, so the payments service holds it for REVIEW |

  The hybrid column was measured with the trained artifacts for amounts INR 100 to 50,000, payee ages 5 s to
  30 min, with and without a device id or a new device, at **every hour of the day** (the model reads the
  hour): 144 combinations per amount, all 0 (ALLOW) for INR 100 and 500 and all exactly 30 for INR 25,000,
  30,000, 40,000 and 50,000.
  The API decides on the higher of the rules and the hybrid score, so neither engine can push these inputs to
  REVIEW or BLOCK. The suite was run green in both modes (see the local results below).
* **Time.** TOTP codes are computed in the test from the key shown on the page (`node:crypto`, RFC 6238, no
  new dependency). The API accepts the previous, current or next 30-second step, each once (replay
  protection), so the `Authenticator` helper always takes a step after the last one used: a journey needs at
  most two codes per customer and never waits for the clock. Staff codes come from
  `scripts/totp.py <email> --unused`, which reads the analyst's last used step from the database and prints
  the earliest step the API has not seen (it only waits, at most one step, if the current and next steps are
  both used). The scheduled transfer starts a week ahead so the worker cannot run it during the test.
* **Assistant without a model.** A clear one-intent question never reaches the LLM: the deterministic router
  picks the read-only account tool and the server writes the reply from its result
  (`modules/assistant/service.py`). The test asserts the balance and the `done` frame
  (`outcome: answered`, `tool: get_account_summary`, `engine: rules`), never model wording, so it passes with
  Ollama on or off.
* **No sleeps, no retries.** Only auto-waiting locators and web-first assertions; no `waitForTimeout`.
  `playwright.config.ts` has `retries: 0`: a test that needs a second attempt is a bug to fix.

## Rate limits

Sign-in (10 a minute) and registration (10 an hour) are limited per client IP, and every Playwright browser
shares one IP: the suite registers 14 customers and signs in more than 20 times. The overlay
`infra/e2e/compose.e2e.yml` raises both limits to 600 for the E2E stack only, the same pattern as
`infra/k6/compose.load.yml` for the load tests; `docker-compose.yml` and the API defaults are unchanged. The
limiter itself is covered by `test_login_rate_limit_per_ip` and the auth integration tests.

## Running locally

```bash
make up                                    # the stack; `make seed` once for the analyst
export PBIP_DEMO_PASSWORD='...'            # the password the stack was seeded with (see below)
docker compose -f docker-compose.yml -f infra/e2e/compose.e2e.yml up -d api   # raised per-IP limits
cd apps/web && npm run test:e2e            # or: make test-e2e
```

`PBIP_DEMO_PASSWORD` is only needed by the fraud-review journey (the seeded `analyst@demo.pbip`). A database
seeded by `make seed` without the variable uses the `DEMO_PASSWORD` constant in `scripts/seed_dev.py` (APP_ENV
local only). `E2E_COMPOSE_DIR` points the helpers at another checkout's running stack (default: this
repository). `make up` restarts the API with the default limits.

## CI

The `e2e` job in `.github/workflows/ci.yml` builds and starts the stack with the overlay, verifies TLS,
then seeds demo customers and MFA-enrolled staff with a password generated for that run:

```yaml
PBIP_DEMO_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(24) + "Aa1!")')"
echo "::add-mask::$PBIP_DEMO_PASSWORD"            # never shown in the log
echo "PBIP_DEMO_PASSWORD=$PBIP_DEMO_PASSWORD" >> "$GITHUB_ENV"   # handed to Playwright
docker compose exec -T -e PBIP_DEMO_PASSWORD api python - < scripts/seed_dev.py
```

`scripts/seed_dev.py` takes the password from `PBIP_DEMO_PASSWORD` and refuses to run without it unless
APP_ENV is `local`; it never prints it. CI has no trained model files, so the API scores with the rules only
(fail-safe), which the journey inputs above were chosen for.

## Local results (2026-11-02, 2-CPU Celeron, stack images from `a0ed6dd`)

| Runs | Engine | Limits | Workers | Result |
|---|---|---|---|---|
| 3 in a row | trained hybrid | `infra/e2e/compose.e2e.yml` | 1 | 16 passed each (1.5, 1.3, 1.3 min) |
| 3 in a row (earlier, same specs) | trained hybrid | same values via environment | 1 | 16 passed each (1.7, 1.2, 1.2 min) |
| 1 | rules only (no model files, as CI), `CI=true` | same | 2 | 16 passed (57.5 s) |

## Limits

* The fraud journeys prove the decisions for the inputs above; the engine's behaviour on other inputs is
  covered by `test_fraud_hybrid.py` and the behaviour-regression suite (TASK-080), not here.
* Statements, bill payments and loans have no E2E journey yet (unit and integration tests only).
* Prompt injection against the assistant (blueprint section 13 lists it for E2E) is covered by the API's
  adversarial suite (`test_assistant_adversarial.py`), not by a browser journey.
* The suite needs Docker on the machine that runs Playwright (for `docker compose exec`).
