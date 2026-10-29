# Property-Based API Fuzzing with Schemathesis

PBIP uses [Schemathesis](https://schemathesis.readthedocs.io/) and Hypothesis for property-based API testing and boundary fuzzing against the authoritative OpenAPI contract at `docs/api/openapi.json`.

## 1. Objectives & Quality Gate

In a regulated banking platform, API contracts must strictly guarantee:
1. **Zero Unhandled HTTP 500 Errors:** Endpoints must reject invalid, malformed, or boundary inputs with appropriate HTTP 4xx errors (e.g. 400 Bad Request, 401 Unauthorized, 403 Forbidden, 422 Unprocessable Entity, 429 Rate Limited), never with unhandled 500 Internal Server Errors.
2. **100% Response Schema Conformance:** Every returned response payload must strictly conform to the declared JSON schema in the OpenAPI specification for the corresponding HTTP status code.
3. **Definition of Done (TASK-070):** The fuzz test executes at least **500 test cases** across API endpoints with zero unhandled 500 errors and zero schema violations.

## 2. Architecture & Components

```
                docs/api/openapi.json
                          │
                          ▼
             schemathesis.openapi.from_path()
                          │
                          ▼
         Hypothesis Parameter & Payload Strategy
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
      [In-Process ASGI]      [Live HTTP Service]
       Isolated FastAPI       http://127.0.0.1:8000
       (FuzzSession +         (PostgreSQL + Redis)
        FakeEngine +
        fakeredis)
              │                       │
              └───────────┬───────────┘
                          ▼
            Property Checks & Assertions:
            - not_a_server_error (no 5xx)
            - response_schema_conformance
```

- **Fuzz Runner (`scripts/fuzz.py`):** Standalone script and library managing test case generation, dispatch, error capture, and telemetry.
- **Developer Command Runner (`scripts/dev.py fuzz`):** Cross-platform CLI entry point for local execution.
- **Unit Contract Test (`apps/api/tests/unit/test_fuzz_contract.py`):** Pytest unit test ensuring the fuzzing harness and OpenAPI specification remain integrated in local test suites.
- **Nightly CI Pipeline (`.github/workflows/fuzz.yml`):** Scheduled GitHub Actions workflow running 500+ test cases nightly against a live containerized database and Redis backend.

## 3. Running Locally

### In-Process Execution (Fast & Dependency-Free)

To run the fuzzing suite locally against the in-process FastAPI application:

```bash
# Using dev.py runner
python scripts/dev.py fuzz --cases 500

# Or via Makefile (Linux/macOS)
make fuzz

# Or via PowerShell (Windows)
.\dev.ps1 fuzz --cases 500
```

### Live Service Fuzzing

To fuzz against a running PBIP API server (e.g., in Docker or local dev server):

```bash
# 1. Start infrastructure and backend
python scripts/dev.py infra-up
python scripts/dev.py migrate
python scripts/dev.py seed
python scripts/dev.py dev-api

# 2. Run fuzzer against live URL
python scripts/dev.py fuzz --url http://127.0.0.1:8000 --cases 500
```

### Command-Line Options

| Option | Type | Default | Description |
|---|---|---|---|
| `--cases` | int | 500 | Minimum number of test cases to generate and execute |
| `--url` | string | `None` | Base URL of running API. If omitted, runs in-process via ASGI |
| `--token` | string | `None` | Optional Bearer token for authenticated endpoint testing |
| `--spec` | path | `docs/api/openapi.json` | Path to OpenAPI JSON contract |
| `--seed` | int | `None` | Hypothesis random seed for reproducible runs |
| `--report-file` | path | `None` | Path to export execution results as JSON |

## 4. Continuous Integration & Scheduled Runs

A dedicated workflow is configured at `.github/workflows/fuzz.yml`:
- **Schedule:** Nightly at 02:00 UTC (`cron: '0 2 * * *'`)
- **Manual Trigger:** Supports `workflow_dispatch` with customizable case counts.
- **Environment:** Provisions PostgreSQL 16 and Redis 7 services, migrates schema, seeds demo data, and executes `scripts/dev.py fuzz --url http://127.0.0.1:8000 --cases 500`.
- **Artifact:** Generates and uploads `fuzz-report.json` containing status distributions and detailed failure records.

## 5. Triage & Failure Diagnosis

When the fuzzer reports a failure:
1. **Unhandled 500 Server Errors:**
   - Inspect the failed endpoint and payload in the test output or `fuzz-report.json`.
   - Identify the unhandled exception (e.g. unhandled boundary value, null dereference, database constraint error).
   - Add appropriate input validation in Pydantic models or domain error handling in the service layer returning RFC 7807 problem details.
2. **Response Schema Violations:**
   - Schemathesis compares the actual response body against the OpenAPI schema defined for that status code.
   - If the API response contains fields not in the schema or violates types, update the router's `response_model` or regenerate the schema via `python scripts/dev.py openapi`.
