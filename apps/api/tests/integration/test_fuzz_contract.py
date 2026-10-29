"""The fuzzer against the real application on PostgreSQL (TASK-070 definition of done, run in CI).

Proves what the nightly job relies on: the runner can register and log in a customer through the
API, so protected operations are exercised past the 401 guard, and schema-valid plus
schema-violating inputs across every operation produce neither 5xx responses nor bodies that
violate the contract.
"""

import pytest
from fastapi import FastAPI

from scripts.fuzz import SPEC_DEFAULT, run_fuzz

pytestmark = pytest.mark.integration


@pytest.mark.timeout(240)
def test_fuzzing_the_real_app_finds_no_5xx_or_schema_violations(app: FastAPI) -> None:
    result = run_fuzz(spec_path=SPEC_DEFAULT, app=app, min_cases=160, seed=2026)

    assert result.is_success, result.failures[:5]
    assert result.server_errors == 0 and result.schema_violations == 0
    assert result.total_cases >= 160
    assert result.authenticated is True
    assert len(result.per_operation) == result.operations_count

    # The customer token worked: an owned-data route answered with data, not 401.
    accounts = result.per_operation["GET /api/v1/accounts"]
    assert accounts.get("200", 0) >= 1, accounts
    # Validation ran on real payloads: schema-violating inputs were rejected, not crashed on.
    assert result.status_codes.get("422", 0) >= 1, result.status_codes
    # The run is not just the auth guard answering: most responses are not 401.
    assert result.status_codes.get("401", 0) * 2 < result.total_cases, result.status_codes
