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

Two checks apply to every response:

1. **`not_a_server_error`:** no 5xx and no transport/ASGI crash. Invalid, boundary or
   adversarial input must be rejected with a 4xx problem+json, never with a stack trace.
2. **`response_schema_conformance`:** the body matches the schema declared for that status code.

**Definition of done (TASK-070):** at least 500 cases with zero unhandled 5xx and zero schema
violations. The runner exits non-zero if either count is non-zero, if any request failed at the
transport level, or if fewer cases than requested were executed.

## 2. Authentication (why it is built in)

Without a bearer token every protected route answers 401 before any validation runs, so an
unauthenticated run only proves that the auth guard comes first. By default the runner therefore
**registers a disposable customer through the API under test** (`POST /api/v1/auth/register`,
then `POST /api/v1/auth/login`) and fuzzes with that customer's token. Before each operation it
checks `GET /api/v1/auth/me` and logs in again if a fuzzed operation invalidated the session.

Limits of this coverage, stated honestly:

* Staff routes require roles and MFA (`amr: otp`); with a customer token they are exercised only
  on their authorization-denied path (403). Pass `--token <staff access token>` to go further.
* `POST /auth/refresh` and `POST /auth/logout` need the CSRF header value `pbip-web`, which the
  generator never produces, so they are always 403 (and have nothing to corrupt in negative mode).
* Routes that take a resource id (`/payees/{id}`, `/transfers/{id}`, ...) get random ids: the fuzz
  customer owns no such resources, so those routes are exercised for validation (422) and the
  404 path, not for their business logic.
* Only `not_a_server_error` and `response_schema_conformance` run. An undocumented status code
  (429 from rate limiting, 400, 403) is therefore not a failure; `status_code_conformance` is not
  enabled because the contract does not document those codes yet.
* Money-moving operations run against the fuzz customer's own fresh account on an ephemeral
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

`--cases` is a floor: every pass covers every operation and mode, so a run usually executes more
cases than requested (two full passes for 500 with the current contract).

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
