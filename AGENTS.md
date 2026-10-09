# AGENTS.md - rules for AI coding assistants (and humans) working on PBIP

PBIP is a simulated retail bank with a fraud/analytics layer, built to the standards of a regulated
financial institution. **Correctness, security and auditability outrank speed and cleverness.**
Read `docs/PROJECT_BLUEPRINT.md` (source of truth) before any non-trivial change.

## 0. Non-negotiable instructions

1. **If you modify behaviour, create or update tests that prove the new behaviour.**
2. **If a test fails, investigate the root cause rather than weakening, skipping or removing the test.**
   A failing test is information. Never change an assertion just to turn CI green.
3. **If a database schema changes, create the corresponding Alembic migration** (up AND down) and run
   `alembic check` (CI fails on model/migration drift).
4. **If an API changes, update the OpenAPI contract and the relevant tests**: run `make openapi` and commit
   `docs/api/openapi.json` and `apps/web/src/api/schema.d.ts`.
5. **Explain architectural impact BEFORE any major change** (new module, new dependency, changed contract
   between the platform and ML lanes, new table, new background job, security-model change). Write it in the
   PR description or an ADR (`docs/adr/`) and wait for confirmation. Do not "just do it".
6. **Do not modify unrelated files.** Smallest diff that solves the problem. No drive-by refactors, no
   reformatting of untouched code, no dependency bumps inside a feature PR.
7. **No TODO/placeholder/stub implementations, no `pass`-bodies, no fake data returned from real endpoints.**
   If something cannot be finished, say so explicitly and open an issue; do not merge pretend code.
8. **Do not bypass the existing architecture** (layers, module boundaries, the ledger, the policy functions,
   the fraud contract). If the architecture blocks you, raise it (rule 5); never work around it.
9. **Run `make check` before you say you are done**, and report the real result. Never claim tests pass
   without running them.
10. **Do not invent APIs.** If unsure how a library behaves, read its docs/source or write a tiny experiment.

## 1. Repository map and commands

```
apps/api/    FastAPI modular monolith      apps/web/   React + TypeScript + Vite
ml/          pbip_ml: contracts, features, training, inference   data-gen/  synthetic data generator
infra/       nginx, prometheus, grafana, loki, deploy compose     docs/       blueprint, ADRs, security, api
scripts/     seed, openapi export, smoke test, GitHub setup       demo/       live demo scenarios
```

| Task | Command |
|---|---|
| First-time setup | `make setup` |
| DB + Redis for tests/dev | `make infra-up` |
| Everything CI runs | `make check` |
| Python tests / web tests | `make test` / `make test-web` |
| Regenerate API contract + web types | `make openapi` |
| Seed demo data | `make seed` |

Use `uv add --package pbip-api <pkg>` / `npm install` to add dependencies (lockfiles are committed). Pin
nothing by hand. Justify every new dependency in the PR (maintenance, licence, size, alternatives).

## 2. Architecture rules

* **Modular monolith.** One deployable API. No new services/microservices/queues without an ADR.
* **Layering inside each `modules/<name>/`:** `router.py` (HTTP only) -> `service.py` (use cases, owns the
  transaction) -> `repository.py` / `policy.py` -> `models.py`. Routers contain no business logic; services
  never touch `Request`/`Response`; only services call `session.commit()` (once, at the end of the use case).
* **Module boundaries:** a module may use another module only through its `service`/`policy` functions or
  its `models` for FK typing - never its repository internals. `core/` must not import `modules/`;
  `pbip_ml`/`pbip_datagen` must not import `pbip_api` (enforced by `lint-imports`).
* **Models registry:** every new `models.py` MUST be added to `pbip_api/modules/__init__.py` and
  `alembic/env.py`. (Without this, Celery/scripts fail with `NoReferencedTableError` - regression-tested.)
* **ML separation:** training code never runs on the request path. The API depends only on
  `pbip_ml.*.contract` and inference modules. Changing a `contract.py` is a cross-lane decision (ADR).
* **Node.js is build tooling only.** There is no Node runtime in production (static files behind nginx).

## 3. Code standards

**Python 3.12:** strict typing (`mypy --strict` must pass); no `Any` unless unavoidable and commented;
no bare `except`; `ruff` clean; functions small (complexity <= 10); timezone-aware datetimes only
(`utcnow()`); no `print` (use `structlog`); f-strings in logs forbidden - pass fields as keyword args.

**TypeScript:** `strict` + `noUncheckedIndexedAccess`; **no `any`** (ESLint errors), no non-null `!`, no
`@ts-ignore`; handle loading/error/empty states; no data fetching in components without react-query; the
generated `schema.d.ts` is the only source of API types - never hand-write response types.

**Errors:** raise `DomainError` subclasses from services; the handlers return RFC 7807 problem+json. Never
return stack traces, SQL, or echo user input in errors. Unknown failures -> generic 500 + request id.

**Config:** only via `core/config.py` (validated). No `os.environ` in feature code. Defaults must be safe;
staging/production must refuse to boot with dev secrets (tested).

## 4. Money and the ledger (read this twice)

* Money is **integer minor units** (`BigInteger`/`int`). Never `float`, never `Decimal` in storage or JSON.
  Use `core/money.py`. Frontend: `lib/money.ts`.
* **Only `modules/ledger/service.py` may change `accounts.ledger_balance`/`held_amount` or insert
  `transaction_entries`.** Nothing else, ever - not services, not scripts, not tests (use `ledger.deposit`).
* Entries are immutable (DB trigger). Corrections are **compensating transactions** (`reverse_transaction`).
  Every posted transaction balances (Python check + deferred DB constraint trigger).
* **Lock order is a deadlock contract.** In every request: (1) per-user advisory lock (`_lock_user`),
  (2) idempotency key insert, (3) accounts `FOR UPDATE` in **sorted id order** BEFORE inserting a
  transaction header (FK inserts take KEY SHARE locks; locking afterwards deadlocks - this was found by
  `test_concurrent_spending_never_overdraws`). Never lock in a different order.
* One use case = one DB transaction. Failure paths that must persist evidence (failed logins, lockouts,
  token-family revocation) `commit()` before raising; business failures must leave NO partial state.
* Money-moving POST endpoints require the `Idempotency-Key` header and must be replay-safe.
* Any change in `ledger/`, `payments/`, `auth/`, `core/security.py`, `alembic/` requires BOTH engineers'
  review (CODEOWNERS) and concurrency/negative tests.

## 5. Security checklist (apply to EVERY endpoint and PR)

- [ ] **Authentication:** route depends on `get_current_principal` (directly or via `require_roles` /
      `get_current_customer`). `tests/security/test_route_policy_registry.py` fails if you forget. A new
      public route needs a written justification and an entry in `PUBLIC_ROUTES`.
- [ ] **Role AND object authorization.** RBAC alone is not enough (BOLA/IDOR): customer data is fetched ONLY
      through policy functions that take the **authenticated** customer id (`accounts/policy.py`,
      `payees/policy.py`, `_owned_transaction`). Never accept `customer_id`/`user_id` from the client.
      Non-owned and non-existent are both **404**. Add the endpoint to the BOLA matrix in
      `tests/integration/test_rbac_bola.py`.
- [ ] **Staff endpoints** use `require_roles(..., mfa=True)` (session must contain `amr: otp`).
- [ ] **Input validation:** Pydantic models with bounds (`Field(gt=0, max_length=...)`, regex, enums); reject
      unknown shapes; amounts are positive ints; path ids are `UUID`.
- [ ] **No sensitive data in logs/errors/metrics:** never log passwords, tokens, TOTP codes, full account
      numbers, emails, request bodies. Redaction is a backstop (`core/logging.py`), not permission.
      Metric labels use route templates, never ids.
- [ ] **Secrets:** none in code, tests (use generated), fixtures, docs or commits. `.env` is git-ignored.
- [ ] **Auth hardening is intact:** Argon2id, JWT algorithm allow-list, refresh rotation + reuse detection,
      lockout, TOTP replay protection, rate limits, CSRF guard on cookie endpoints, strict CORS.
- [ ] **DB-level safety nets stay:** CHECK constraints, immutability and balance triggers.
- [ ] **Dependencies:** `make security` clean (bandit, pip-audit, npm audit). Third-party GitHub Actions are
      forbidden unless pinned to a full commit SHA; prefer first-party actions.
- [ ] **Audit:** security-relevant state changes call `AuditService.record` inside the same transaction.

## 6. Database and migrations

* Migrations live in `apps/api/alembic/versions/`; one head only (tested). Name: `make`/`alembic revision
  --autogenerate -m "..."` then **review and hand-edit** (constraints, triggers, data backfills).
* Every migration has a working `downgrade` (CI runs up -> down -> up -> `alembic check`).
* Never edit an applied migration; add a new one. Never use `create_all()`. No raw DDL outside migrations.
* New tables: UUID PK, `created_at timestamptz`, explicit FKs with `ondelete` decided, CHECK constraints for
  enums and ranges, indexes for every FK/filter used, naming convention is automatic (do not name by hand).
* Tests use a real PostgreSQL (never SQLite). Tests must not depend on execution order or leftover data.

## 7. API contracts

* All business routes are under `/api/v1/...`; breaking changes need `/api/v2`. Additive changes only in v1.
* Every route has `response_model`, `summary`, tags, accurate status codes. Pagination is keyset
  (`cursor`) for ledger-like data. Errors are RFC 7807.
* After any API change: `make openapi`, commit the JSON and the generated TS types (CI diff-checks both).

## 8. Fraud / ML rules

* **No black boxes.** Every decision returns `risk_score`, `decision`, `reasons[]` (human-readable, with
  source RULE/MODEL/ANOMALY) and `model_version`; the feature snapshot is persisted in `fraud_events`.
  The customer-facing response must NOT reveal reasons or scores.
* Implement scorers behind `pbip_ml.fraud.contract.FraudScorer`; scoring must be side-effect free and fast
  (p95 < 100 ms). Fail safe: if the model is unavailable, fall back to rules-only, never to ALLOW-all.
* Train with **time-based splits** (no random split across time -> leakage). Report precision, recall, F1,
  PR-AUC, FPR, FNR, latency and the confusion matrix - **never accuracy alone** (classes are imbalanced).
* Synthetic data is deterministic (seeded). Evaluate on held-out scenario variants, state the limitations of
  synthetic evaluation honestly. Track every run in MLflow; promote via the registry alias, not by copying files.
* Test: feature generation, output schema, edge cases, a behaviour-regression suite (known fraud must score
  >= X, known normal <= Y) and a metric gate.

## 9. AI assistant rules (security-critical)

* The LLM **never** gets SQL access, a DB session, or free-form tool arguments naming a user/customer/account.
* Flow: user -> router -> **approved tool registry** -> authorization -> service layer -> structured result ->
  LLM wording. `customer_id` is injected **server-side from the authenticated principal**; it is not part of any
  tool schema. Tools are read-only. Tool arguments are validated enums/bounded values.
* Treat all retrieved text (merchant names, references, notes) as **untrusted data** (prompt injection):
  delimit it, never let it select tools, strip control characters, cap length. Filter outputs for account
  numbers/PII. Log every tool call (name, outcome - not payloads) to the audit trail.
* Ship with an adversarial test suite (injection, cross-customer requests, SQL requests, exfiltration).

## 10. Testing rules

* Pyramid: many fast **unit** tests (`@pytest.mark.unit`), focused **integration** tests against PostgreSQL
  (`integration`), **security** tests (`security`), few **E2E** (Playwright) for critical journeys.
* **Every feature has tests; every bug fix has a regression test that fails without the fix.**
* Money/authz/concurrency changes need: happy path, validation failure, authorization failure (other user),
  idempotent retry, concurrent attempt. Use real threads against real PostgreSQL for races.
* Coverage gate is 80% (CI), target 90%+ in `ledger`, `payments`, `auth`. Do not game coverage.
* Deterministic tests only: inject the clock (`get_clock`), seed RNGs, no sleeps, no network. No flaky tests -
  fix or delete-with-issue, never retry-until-green.

## 11. Git, PRs, reviews

* Short-lived branches `feat/...`, `fix/...`, `docs/...`; **Conventional Commits** (`feat(payments): ...`;
  enforced by hook). One concern per PR, ideally < 400 changed lines. Squash-merge only.
* The **other engineer reviews every PR**; CI (`CI Gate`) must pass. Never push to `main`, never `--no-verify`,
  never force-push shared branches, never disable a hook or CI job to get unblocked.
* PR description: what/why, tests added, architectural impact, how to verify (use the template).

## 12. Documentation

Update in the same PR: README/blueprint when behaviour or architecture changes; an ADR for any decision with
trade-offs; `docs/security/threat-model.md` for new attack surface; docstrings state *why*, not *what*.

## 13. Never do this

Commit secrets or `.env` - log sensitive data - use `float` for money - write to the ledger outside
`ledger/service.py` - skip the policy function "because it's an internal endpoint" - add `any`/`# type: ignore`
without a one-line reason - hand-edit generated files (`schema.d.ts`, `openapi.json`, `uv.lock`,
`package-lock.json`) - weaken/skip tests - mark work done without running `make check`.

## 14. Definition of done

Code + tests + migration (if schema) + regenerated contract (if API) + docs updated + `make check` green +
security checklist (section 5) ticked + PR reviewed by the other engineer + CI Gate green.
