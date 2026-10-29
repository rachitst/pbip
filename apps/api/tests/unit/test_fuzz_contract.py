"""Unit test validating Schemathesis property-based API fuzzing contract and failure modes."""

import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import pytest

from scripts.fuzz import (
    FuzzFailure,
    FuzzResult,
    _record_outcome,
    build_isolated_app,
    main,
    run_fuzz,
)

pytestmark = pytest.mark.unit

SPEC_PATH = Path(__file__).resolve().parents[4] / "docs" / "api" / "openapi.json"


def _make_dummy_result(
    server_errors: int = 0,
    schema_violations: int = 0,
    failures: list[FuzzFailure] | None = None,
    total_cases: int = 10,
) -> FuzzResult:
    failure_objs = failures or []
    return FuzzResult(
        spec_path="docs/api/openapi.json",
        target="test-target",
        total_cases=total_cases,
        operations_count=5,
        passed_cases=total_cases - len(failure_objs),
        server_errors=server_errors,
        schema_violations=schema_violations,
        status_codes={"200": total_cases},
        duration_seconds=1.0,
        failures=[asdict(f) for f in failure_objs],
    )


def test_openapi_spec_exists() -> None:
    """Verify that committed OpenAPI specification file exists."""
    assert SPEC_PATH.exists(), f"Missing OpenAPI specification at {SPEC_PATH}"


def test_isolated_app_builds_cleanly() -> None:
    """Verify isolated test FastAPI app initializes with test doubles."""
    app = build_isolated_app()
    assert app is not None
    assert hasattr(app, "dependency_overrides")


def test_property_based_fuzz_contract_compliance() -> None:
    """Exercise API operations with Schemathesis and verify schema conformance.

    Asserts zero unhandled HTTP 500 errors, zero schema violations, and zero failures.
    """
    result = run_fuzz(
        spec_path=SPEC_PATH,
        min_cases=38,
        seed=42,
    )

    assert result.is_success, f"Fuzzing detected failures: {result.failures}"
    assert result.server_errors == 0, f"Detected unhandled 500 errors: {result.failures}"
    assert result.schema_violations == 0, f"Detected schema violations: {result.failures}"
    assert len(result.failures) == 0, f"Unexpected failures in successful run: {result.failures}"
    assert result.passed_cases == result.total_cases
    assert result.total_cases >= 38
    assert result.operations_count >= 30


def test_regression_transport_failure_causes_failure_and_nonzero_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: status code 0 transport failures must fail is_success and exit nonzero."""
    transport_failure = FuzzFailure(
        method="GET",
        path="/api/health/live",
        status_code=0,
        error_type="NetworkOrTransportError",
        message="Connection reset by peer",
    )
    result = _make_dummy_result(
        server_errors=0,
        schema_violations=0,
        failures=[transport_failure],
    )

    assert not result.is_success, (
        "Transport failure with status 0 must cause is_success to be False"
    )
    assert result.passed_cases < result.total_cases

    monkeypatch.setattr(sys, "argv", ["fuzz.py", "--cases", "10"])
    monkeypatch.setattr("scripts.fuzz.run_fuzz", lambda **_kw: result)
    exit_code = main()
    assert exit_code == 1, "CLI must return nonzero exit code on transport failure"


def test_http_5xx_failure_causes_failure_and_nonzero_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unhandled HTTP 500 server errors must fail is_success and exit nonzero."""
    server_error = FuzzFailure(
        method="POST",
        path="/api/v1/auth/login",
        status_code=500,
        error_type="UnhandledServerError",
        message="500 Internal Server Error",
    )
    result = _make_dummy_result(
        server_errors=1,
        schema_violations=0,
        failures=[server_error],
    )

    assert not result.is_success, "HTTP 500 server error must cause is_success to be False"
    assert result.server_errors == 1

    monkeypatch.setattr(sys, "argv", ["fuzz.py", "--cases", "10"])
    monkeypatch.setattr("scripts.fuzz.run_fuzz", lambda **_kw: result)
    exit_code = main()
    assert exit_code == 1, "CLI must return nonzero exit code on 500 server error"


def test_schema_violation_causes_failure_and_nonzero_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Response schema violations must fail is_success and exit nonzero."""
    schema_violation = FuzzFailure(
        method="GET",
        path="/api/v1/accounts",
        status_code=200,
        error_type="ResponseSchemaViolation",
        message="Missing required property: ledger_balance",
    )
    result = _make_dummy_result(
        server_errors=0,
        schema_violations=1,
        failures=[schema_violation],
    )

    assert not result.is_success, "Schema violation must cause is_success to be False"
    assert result.schema_violations == 1

    monkeypatch.setattr(sys, "argv", ["fuzz.py", "--cases", "10"])
    monkeypatch.setattr("scripts.fuzz.run_fuzz", lambda **_kw: result)
    exit_code = main()
    assert exit_code == 1, "CLI must return nonzero exit code on schema violation"


@pytest.mark.parametrize("status_code", [401, 403, 422, 429])
def test_expected_client_responses_are_not_treated_as_failures(status_code: int) -> None:
    """Expected HTTP 4xx client errors that conform to OpenAPI contract must not be failures."""
    status_counts: Counter[str] = Counter()
    failures: list[FuzzFailure] = []

    s_err, sch_err = _record_outcome(
        status=status_code,
        failure=None,
        status_counts=status_counts,
        failures=failures,
    )

    assert s_err == 0
    assert sch_err == 0
    assert len(failures) == 0
    assert status_counts[str(status_code)] == 1
