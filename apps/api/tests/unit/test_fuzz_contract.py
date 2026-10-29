"""Unit tests for ``scripts/fuzz.py``: generation, bookkeeping and exit codes, without a database.

The runner's real target (the application on PostgreSQL) is exercised in
``tests/integration/test_fuzz_contract.py``. Here the request executor is stubbed, so the tests are
fast and deterministic while still driving real Schemathesis/Hypothesis generation from the
committed OpenAPI contract.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.fuzz import (
    GENERATION_MODES,
    FuzzFailure,
    FuzzIdentity,
    FuzzResult,
    Target,
    _make_executor,
    _Tally,
    main,
    run_fuzz,
)

pytestmark = pytest.mark.unit

SPEC_PATH = Path(__file__).resolve().parents[4] / "docs" / "api" / "openapi.json"
NOT_AN_APP = object()  # the stubbed executor never touches the target


def _ok(_case: object, _headers: dict[str, str]) -> tuple[int, FuzzFailure | None]:
    return 200, None


def _case(method: str = "GET", path: str = "/api/v1/accounts") -> Any:
    """The attributes of a Schemathesis Case that the tally reads."""
    return SimpleNamespace(
        method=method, path=path, path_parameters=None, query=None, headers=None, body=None
    )


def _result(failures: list[FuzzFailure], total_cases: int = 10) -> FuzzResult:
    server_errors = sum(1 for f in failures if f.status_code >= 500)
    violations = sum(1 for f in failures if f.error_type == "ResponseSchemaViolation")
    return FuzzResult(
        spec_path="docs/api/openapi.json",
        target="test-target",
        authenticated=True,
        generation_modes=list(GENERATION_MODES),
        seed=1,
        total_cases=total_cases,
        operations_count=5,
        passed_cases=total_cases - len(failures),
        server_errors=server_errors,
        schema_violations=violations,
        status_codes={"200": total_cases},
        per_operation={},
        unsatisfiable=[],
        input_digest="0" * 64,
        duration_seconds=1.0,
        failures=[f.__dict__ for f in failures],
    )


def test_same_seed_reproduces_the_inputs_and_every_operation_is_fuzzed_in_both_modes() -> None:
    first = run_fuzz(
        spec_path=SPEC_PATH, app=NOT_AN_APP, auth=False, min_cases=80, seed=11, execute=_ok
    )
    again = run_fuzz(
        spec_path=SPEC_PATH, app=NOT_AN_APP, auth=False, min_cases=80, seed=11, execute=_ok
    )
    other = run_fuzz(
        spec_path=SPEC_PATH, app=NOT_AN_APP, auth=False, min_cases=80, seed=12, execute=_ok
    )

    assert first.input_digest == again.input_digest
    assert first.input_digest != other.input_digest
    assert first.total_cases >= 80 and first.operations_count >= 30
    assert first.seed == 11 and first.generation_modes == ["positive", "negative"]
    assert len(first.per_operation) == first.operations_count  # every operation got requests
    # Only operations without any input to corrupt are skipped, and only in negative mode.
    assert first.unsatisfiable and all(u.startswith("negative ") for u in first.unsatisfiable)
    assert first.is_success and first.authenticated is False


def test_bearer_token_is_sent_with_every_request() -> None:
    seen: set[str | None] = set()

    def spy(_case: object, headers: dict[str, str]) -> tuple[int, FuzzFailure | None]:
        seen.add(headers.get("Authorization"))
        return 200, None

    result = run_fuzz(
        spec_path=SPEC_PATH, app=NOT_AN_APP, bearer_token="t0k", min_cases=40, seed=1, execute=spy
    )
    assert seen == {"Bearer t0k"}
    assert result.authenticated is True


def test_tally_classifies_server_errors_schema_violations_and_transport_failures() -> None:
    tally = _Tally(goal=10)
    tally.record(_case(), 200, None)
    tally.record(
        _case("POST", "/api/v1/auth/login"),
        500,
        FuzzFailure("POST", "/api/v1/auth/login", 500, "UnhandledServerError", "boom"),
    )
    tally.record(
        _case(), 200, FuzzFailure("GET", "/api/v1/accounts", 200, "ResponseSchemaViolation", "x")
    )
    tally.record(
        _case("POST", "/api/v1/auth/register"),
        0,
        FuzzFailure("POST", "/api/v1/auth/register", 0, "NetworkOrTransportError", "reset"),
    )

    assert tally.executed == 4
    assert tally.server_errors == 1 and tally.schema_violations == 1 and len(tally.failures) == 3
    assert tally.status_counts == {"200": 2, "500": 1}  # a transport failure has no status
    assert tally.per_operation["POST /api/v1/auth/register"]["0"] == 1
    assert len(tally.input_digest) == 64


@pytest.mark.parametrize("status_code", [401, 403, 404, 409, 422, 429])
def test_expected_client_responses_are_not_failures(status_code: int) -> None:
    tally = _Tally(goal=1)
    tally.record(_case(), status_code, None)
    assert not tally.failures and tally.server_errors == 0 and tally.schema_violations == 0
    assert tally.status_counts[str(status_code)] == 1


def test_any_failure_kind_makes_the_run_unsuccessful() -> None:
    transport = FuzzFailure("GET", "/api/health/live", 0, "NetworkOrTransportError", "reset")
    server = FuzzFailure("POST", "/api/v1/auth/login", 500, "UnhandledServerError", "boom")
    schema = FuzzFailure("GET", "/api/v1/accounts", 200, "ResponseSchemaViolation", "missing")
    for failure in (transport, server, schema):
        assert not _result([failure]).is_success, failure.error_type
    assert _result([]).is_success


def test_cli_exit_code_and_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = tmp_path / "out" / "fuzz-report.json"
    calls: list[dict[str, Any]] = []

    def fake_run_fuzz(**kwargs: Any) -> FuzzResult:
        calls.append(kwargs)
        return outcome

    monkeypatch.setattr("scripts.fuzz.run_fuzz", fake_run_fuzz)
    argv = ["fuzz.py", "--cases", "10", "--seed", "5", "--mode", "negative", "--report-file"]
    monkeypatch.setattr(sys, "argv", [*argv, str(report)])

    outcome = _result([])
    assert main() == 0
    assert calls[-1]["seed"] == 5 and calls[-1]["modes"] == ("negative",)
    assert calls[-1]["auth"] is True
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["total_cases"] == 10 and written["seed"] == 1 and written["failures"] == []

    outcome = _result([], total_cases=9)  # fewer cases than requested is a failed run
    assert main() == 1
    outcome = _result([FuzzFailure("GET", "/x", 500, "UnhandledServerError", "boom")])
    assert main() == 1


class _FakeTarget(Target):
    """Register and login succeed; the session is reported invalid once (e.g. after a logout)."""

    def __init__(self, register_status: int = 201) -> None:
        super().__init__(base_url="http://fuzz.invalid")
        self.register_status = register_status
        self.logins = 0
        self.me_statuses = [401, 200]

    def post_json(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if path.endswith("/register"):
            assert payload["email"].startswith("fuzz-") and len(payload["password"]) >= 12
            return self.register_status, {}
        self.logins += 1
        return 200, {"access_token": f"token-{self.logins}"}

    def get_status(self, _path: str, headers: dict[str, str]) -> int:
        assert headers["Authorization"].startswith("Bearer token-")
        return self.me_statuses.pop(0)


def test_identity_registers_then_logs_in_again_only_when_the_session_became_invalid() -> None:
    target = _FakeTarget()
    identity = FuzzIdentity(target)

    assert identity.provision() == {"Authorization": "Bearer token-1"}
    assert identity.ensure() == {"Authorization": "Bearer token-2"}  # /me said 401
    assert identity.ensure() == {"Authorization": "Bearer token-2"}  # /me said 200
    assert target.logins == 2


def test_identity_fails_clearly_when_registration_is_refused() -> None:
    with pytest.raises(RuntimeError, match="HTTP 429"):
        FuzzIdentity(_FakeTarget(register_status=429)).provision()


def test_target_needs_exactly_one_of_url_or_app() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        Target()
    with pytest.raises(ValueError, match="exactly one"):
        Target(base_url="http://x", app=NOT_AN_APP)


class _FailingCase:
    """A case whose request gets a 500 and whose checks report it the way Schemathesis does."""

    method, path, path_parameters, query, headers, body = (
        "GET",
        "/api/v1/notifications",
        None,
        None,
        None,
        None,
    )

    def call(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(status_code=500)

    def validate_response(self, _response: Any, **_kwargs: Any) -> None:
        # schemathesis.core.failures.FailureGroup derives from BaseExceptionGroup, not Exception.
        raise BaseExceptionGroup(
            "Schemathesis found 1 distinct failure", [AssertionError("Server error")]
        )


def test_a_failed_check_is_recorded_not_raised() -> None:
    """Regression: a 5xx found by Schemathesis aborted the whole run instead of being a finding."""
    execute = _make_executor(Target(base_url="http://fuzz.invalid"))

    status, failure = execute(_FailingCase(), {})  # type: ignore[arg-type]  # minimal Case double

    assert status == 500 and failure is not None
    assert failure.error_type == "UnhandledServerError"
    assert "Server error" in failure.message and failure.path == "/api/v1/notifications"
