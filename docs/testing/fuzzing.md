# Property-Based API Fuzzing with Schemathesis

PBIP fuzzes its API with [Schemathesis](https://schemathesis.readthedocs.io/) and Hypothesis,
driven by the committed OpenAPI contract `docs/api/openapi.json` (TASK-070). The runner is
`scripts/fuzz.py`; the nightly job is `.github/workflows/fuzz.yml`.

## 1. What a run proves

Every operation in the contract receives generated requests in two modes:

| Mode | Inputs | What it exercises |
|---|---|---|
| `positive` | schema-valid path/query/header/body values | the service layer behind validation |
| `negative` | values that violate the schema (wrong types, out-of-range, missing fields) | validation and error handling |

Three checks apply to every response:

1. **`not_a_server_error`:** no 5xx and no transport/ASGI crash. Invalid, boundary or
   adversarial input must be rejected with a 4xx problem+json, never with a stack trace.
2. **`status_code_conformance`** (#175 M11): the status code is documented for that operation.
3. **`response_schema_conformance`:** the body matches the schema declared for that status code.

**Definition of done (TASK-070):** at least 500 cases with zero unhandled 5xx, zero undocumented
status codes and zero schema violations. The runner exits non-zero if any count is non-zero, if
any request failed at the transport level, or if fewer cases than requested were executed.

**How the contract documents shared error codes.** 401, 403, 429 and 400 come from shared
dependencies, not from a route's own code, so `core.errors.document_problem_responses` adds them
from each route's dependency tree when the app is built: 401 where the principal is required, 403
behind a role gate (customer-only, staff `require_roles`) or the CSRF guard, 429 behind a rate
limiter, and 400 (unparseable JSON) on routes with a body. Codes a route raises itself (409, 404
for an id in the body or query, 423 lockout) are in that route's `responses=`.
`apps/api/tests/unit/test_openapi_problem_responses.py` checks the committed contract.

## 2. Authentication (why it is built in)

Without a bearer token every protected route answers 401 before any validation runs, so an
unauthenticated run only proves that the auth guard comes first. By default the runner therefore
**registers a disposable customer through the API under test** (`POST /api/v1/auth/register`,
then `POST /api/v1/auth/login`) and fuzzes with that customer's token. Before each operation it
checks `GET /api/v1/auth/me` and logs in again if a fuzzed operation invalidated the session.

**Owned resources and staff (#175 M11).** After the customer signs in, the runner seeds what the
customer owns, through the API (a second account, a payee, a device, a transfer between the two
accounts, a scheduled transfer) except the opening deposit, which goes through `ledger.deposit` on
the target's database like every seed. In `positive` mode about half of the generated path ids
(`account_id`, `payee_id`, `device_id`, `transfer_id`, `scheduled_transfer_id`, and `customer_id` /
`user_id` for staff routes) are swapped for owned ones, chosen from the generated value so a seed
still reproduces the run; the other half keep the random id and exercise the 404 path. No public
API creates staff, so the runner also **bootstraps an MFA-enrolled ADMIN through the service layer**
on the target's database (as `scripts/seed_dev.py` does) and signs it in through the API with a TOTP
code, so its session carries `amr: otp`. ADMIN passes every staff gate. Operations behind a
`require_roles` gate (read from the app's routers) use that session; the rest use the customer's.

This needs the target's database: in-process it is the app's own; with `--url` it is
`--database-url` (default `TEST_DATABASE_URL`, which the nightly job sets to the server's
database), and the server's `FIELD_ENCRYPTION_KEY` in the environment. `--no-harness` keeps the
customer-only coverage.

Limits of this coverage, stated honestly:

* One staff identity (ADMIN). Role-specific denials (an analyst on an admin route) are covered by
  the RBAC tests, not here.
* `POST /auth/refresh` and `POST /auth/logout` need the CSRF header value `pbip-web`, which the
  generator never produces, so they are always 403 (and have nothing to corrupt in negative mode).
* Fraud alerts are not seeded, so `/fraud/alerts/{alert_id}` routes see only random ids (404).
* Money-moving operations run against the fuzz customer's own fresh accounts on an ephemeral
  database (CI service container or your local test database). Never point `--url` at a shared
  environment with real data.

`--no-auth` fuzzes the unauthenticated surface only.

## 3. Targets

| Target | Flag | Needs |
|---|---|---|
| Live server | `--url http://127.0.0.1:8000` | a running API (what the nightly job does) |
| In-process app | default | a migrated PostgreSQL via `--database-url` or `TEST_DATABASE_URL` (as for `make test-int`); Redis is replaced by `fakeredis` |

The in-process target is the real application on a real database; there is no mocked session,
because a mocked persistence layer can itself crash and would misreport its own failures as API
defects.

## 4. Running locally

```bash
# In-process, against the local test database (make infra-up; migrations are applied by the app's
# integration fixtures, or run `python scripts/dev.py migrate` with TEST_DATABASE_URL first)
python scripts/dev.py fuzz --cases 500 --seed 7

# Against a running server
python scripts/dev.py infra-up && python scripts/dev.py migrate && python scripts/dev.py dev-api
python scripts/dev.py fuzz --url http://127.0.0.1:8000 --cases 500 --report-file fuzz-report.json

# Windows
.\dev.ps1 fuzz --cases 500
```

| Option | Default | Description |
|---|---|---|
| `--cases` | 500 | Minimum number of cases to execute |
| `--url` | in-process | Base URL of a running API |
| `--database-url` | `TEST_DATABASE_URL` | PostgreSQL for the in-process target |
| `--no-auth` | off | Do not register a fuzz customer |
| `--token` | none | Bearer token to use instead of the registered customer |
| `--no-harness` | off | Do not seed owned resources or bootstrap the staff user |
| `--mode` | `both` | `positive`, `negative` or `both` |
| `--seed` | random | Hypothesis seed; the same seed generates the same inputs |
| `--spec` | `docs/api/openapi.json` | OpenAPI contract |
| `--report-file` | none | Write the JSON report |

## 5. Reproducing a failure

The report (console and `--report-file`) contains, per failure, the method, path, status, check
message and the generated request (path parameters, query, headers without `Authorization`, body).
It also contains the **seed** and an **input digest** (SHA-256 over every generated request,
with unset values normalised): the same seed, contract and library versions reproduce the same
inputs, and equal digests confirm it across processes. The nightly job uses the workflow run number
as the seed, so a failing night is replayed with `python scripts/dev.py fuzz --url ... --seed <run
number>`; the uploaded artifact also holds `api-server.log` with the server-side traceback.

`--cases` is a floor: the first pass covers every operation and mode, so a run executes at least one
case per operation and mode, and may run more than requested when that pass alone exceeds the floor
(578 for 500 with the current contract). When the first pass falls short, top-up passes are sized to
the shortfall and stop once the floor is reached, instead of repeating a full pass.

Ordinary unit tests cover the runner itself (`apps/api/tests/unit/test_fuzz_contract.py`, no
database, stubbed executor), and `apps/api/tests/integration/test_fuzz_contract.py` runs the real
application on PostgreSQL in the CI `api-tests` job with a fixed seed.

## 6. Nightly job

`.github/workflows/fuzz.yml` runs at 02:00 UTC and on `workflow_dispatch` (input `cases`). It
provisions PostgreSQL 16 and Redis 7, applies migrations, seeds demo data, starts the API, waits for
`/api/health/ready`, runs 500+ authenticated cases in both modes, and uploads `fuzz-report.json`.

## 7. Triage

1. **Unhandled 5xx or transport failure:** take the request from the report, reproduce it with the
   seed, find the unhandled exception, and add validation (Pydantic bounds) or a `DomainError`
   mapped to problem+json. Add a regression test that fails without the fix.
2. **Schema violation:** the response body does not match the contract for that status. Fix the
   `response_model` (or the error handler) and regenerate the contract with
   `python scripts/dev.py openapi`; never hand-edit `openapi.json`.
3. **"Not generated" entries** list operations that have no input to corrupt in negative mode
   (for example `GET /api/health/live`); they are informational, not failures.
