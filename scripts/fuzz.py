#!/usr/bin/env python3
"""Property-based API fuzzing runner using Schemathesis for PBIP.

Validates the PBIP OpenAPI contract against boundary cases and verifies zero
unhandled HTTP 500 server errors and 100% response schema conformance.

Usage:
    python scripts/fuzz.py [--cases 500] [--url http://127.0.0.1:8000]
    python scripts/dev.py fuzz [--cases 500]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import warnings
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hypothesis.errors import NonInteractiveExampleWarning

warnings.filterwarnings("ignore", category=NonInteractiveExampleWarning)

ROOT = Path(__file__).resolve().parents[1]
SPEC_DEFAULT = ROOT / "docs" / "api" / "openapi.json"

# Ensure apps/api/src and apps/api/tests are in sys.path
for extra_path in (ROOT / "apps" / "api" / "src", ROOT / "apps" / "api" / "tests"):
    if str(extra_path) not in sys.path:
        sys.path.insert(0, str(extra_path))


@dataclass
class FuzzFailure:
    """Record of a failed test case during fuzzing."""

    method: str
    path: str
    status_code: int
    error_type: str
    message: str
    request_data: dict[str, Any] | None = None


@dataclass
class FuzzResult:
    """Aggregated results of the fuzzing test execution."""

    spec_path: str
    target: str
    total_cases: int
    operations_count: int
    passed_cases: int
    server_errors: int
    schema_violations: int
    status_codes: dict[str, int]
    duration_seconds: float
    failures: list[dict[str, Any]]

    @property
    def is_success(self) -> bool:
        return self.server_errors == 0 and self.schema_violations == 0 and len(self.failures) == 0


class FakeResult:
    """Mock query result for isolated fuzzing."""

    def scalar_one_or_none(self) -> None:
        return None

    def scalar_one(self) -> None:
        return None

    def first(self) -> None:
        return None

    def all(self) -> list[Any]:
        return []

    def scalars(self) -> FakeResult:
        return self


class FuzzSession:
    """Lightweight mock session for in-process unauthenticated fuzzing."""

    def __enter__(self) -> FuzzSession:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: Any, **_kwargs: Any) -> FakeResult:
        return FakeResult()

    def add(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def build_isolated_app() -> Any:
    """Construct an isolated FastAPI app instance for in-process fuzzing."""
    import fakeredis
    from cryptography.fernet import Fernet
    from pbip_api.core.config import Settings, get_settings
    from pbip_api.core.crypto import FieldCipher
    from pbip_api.core.db import get_engine, get_session
    from pbip_api.core.redis import get_redis
    from pbip_api.main import create_app
    from pbip_api.modules.auth.dependencies import get_field_cipher
    from pbip_test_support import FakeEngine

    key = Fernet.generate_key().decode()
    cipher = FieldCipher(key)
    fake_redis = fakeredis.FakeRedis()
    settings = Settings(
        app_env="local",
        field_encryption_key=key,
        log_level="ERROR",
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_field_cipher] = lambda: cipher
    app.dependency_overrides[get_redis] = lambda: fake_redis
    app.dependency_overrides[get_engine] = lambda: FakeEngine(healthy=True)
    app.dependency_overrides[get_session] = lambda: FuzzSession()
    return app


def _extract_request_data(case: Any) -> dict[str, Any]:
    raw_headers = getattr(case, "headers", {}) or {}
    redacted = {k: v for k, v in raw_headers.items() if k.lower() != "authorization"}
    return {
        "query": getattr(case, "query", None),
        "headers": redacted,
        "body": getattr(case, "body", None),
    }


def _execute_case(
    case: Any,
    app: Any | None,
    base_url: str | None,
    headers: dict[str, str],
) -> tuple[int, FuzzFailure | None]:
    """Execute a single generated case and validate response conformance."""
    from schemathesis.checks import not_a_server_error, response_schema_conformance

    req_headers = headers if headers else None
    try:
        if base_url:
            resp = case.call(base_url=base_url, headers=req_headers)
        else:
            resp = case.call(app=app, headers=req_headers)
    except Exception as exc:
        failure = FuzzFailure(
            method=case.method,
            path=case.path,
            status_code=0,
            error_type="NetworkOrTransportError",
            message=str(exc),
        )
        return 0, failure

    status = resp.status_code
    try:
        case.validate_response(
            resp,
            checks=[not_a_server_error, response_schema_conformance],
        )
    except Exception as exc:
        err_type = "UnhandledServerError" if status >= 500 else "ResponseSchemaViolation"
        failure = FuzzFailure(
            method=case.method,
            path=case.path,
            status_code=status,
            error_type=err_type,
            message=str(exc),
            request_data=_extract_request_data(case),
        )
        return status, failure

    return status, None


def _load_operations(spec_path: Path) -> tuple[Any, list[Any]]:
    """Load schema and filter valid OpenAPI operations."""
    import schemathesis
    from schemathesis.core.result import Ok

    if not spec_path.exists():
        msg = f"OpenAPI specification not found: {spec_path}"
        raise FileNotFoundError(msg)

    schema = schemathesis.openapi.from_path(str(spec_path))
    raw_ops = list(schema.get_all_operations())
    operations = [r.ok() for r in raw_ops if isinstance(r, Ok)]

    if not operations:
        msg = f"No valid OpenAPI operations found in {spec_path}"
        raise RuntimeError(msg)

    return schema, operations


def _process_operation(
    op: Any,
    strategy: Any,
    app: Any | None,
    base_url: str | None,
    headers: dict[str, str],
) -> tuple[int, FuzzFailure | None]:
    try:
        case = strategy.example()
    except Exception as exc:
        sys.stderr.write(f"Failed generating example for {op.method.upper()} {op.path}: {exc}\n")
        return 0, None
    return _execute_case(case, app, base_url, headers)


def _record_outcome(
    status: int,
    failure: FuzzFailure | None,
    status_counts: Counter[str],
    failures: list[FuzzFailure],
) -> tuple[int, int]:
    """Record status and failure metrics, returning (server_err_delta, schema_err_delta)."""
    if status > 0:
        status_counts[str(status)] += 1
    if not failure:
        return 0, 0
    failures.append(failure)
    if failure.status_code >= 500:
        return 1, 0
    if failure.error_type == "ResponseSchemaViolation":
        return 0, 1
    return 0, 0


def run_fuzz(
    spec_path: Path = SPEC_DEFAULT,
    base_url: str | None = None,
    min_cases: int = 500,
    bearer_token: str | None = None,
    seed: int | None = None,
) -> FuzzResult:
    """Run property-based fuzzing against OpenAPI operations."""
    import hypothesis

    if seed is not None:
        hypothesis.seed(seed)

    logging.getLogger("pbip_api").setLevel(logging.ERROR)
    logging.getLogger("schemathesis").setLevel(logging.WARNING)

    _, operations = _load_operations(spec_path)
    app = build_isolated_app() if not base_url else None
    target_desc = base_url if base_url else "in-process ASGI app"

    sys.stdout.write(f"Loaded {len(operations)} operations from {spec_path.relative_to(ROOT)}\n")
    sys.stdout.write(f"Fuzzing target: {target_desc} (target cases: {min_cases})\n")
    sys.stdout.flush()

    status_counts: Counter[str] = Counter()
    failures: list[FuzzFailure] = []
    server_error_count = 0
    schema_violation_count = 0
    executed_cases = 0

    started = time.perf_counter()
    headers: dict[str, str] = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
    strategies = [(op, op.as_strategy()) for op in operations]
    cases_per_op = max(1, (min_cases + len(operations) - 1) // len(operations))

    for cycle in range(cases_per_op):
        for op, strategy in strategies:
            if executed_cases >= min_cases and cycle >= 1:
                break

            status, failure = _process_operation(op, strategy, app, base_url, headers)
            if status == 0 and failure is None:
                continue

            s_err, sch_err = _record_outcome(status, failure, status_counts, failures)
            server_error_count += s_err
            schema_violation_count += sch_err

            executed_cases += 1
            if executed_cases % 100 == 0 or executed_cases == min_cases:
                sys.stdout.write(
                    f"Progress: {executed_cases}/{min_cases} cases executed "
                    f"({server_error_count} 500s, {schema_violation_count} schema errors)\n"
                )
                sys.stdout.flush()

        if executed_cases >= min_cases:
            break

    elapsed = time.perf_counter() - started
    return FuzzResult(
        spec_path=str(spec_path.relative_to(ROOT)),
        target=target_desc,
        total_cases=executed_cases,
        operations_count=len(operations),
        passed_cases=executed_cases - len(failures),
        server_errors=server_error_count,
        schema_violations=schema_violation_count,
        status_codes=dict(sorted(status_counts.items())),
        duration_seconds=round(elapsed, 2),
        failures=[asdict(f) for f in failures],
    )


def print_summary(res: FuzzResult) -> None:
    """Print readable summary of the fuzzing run."""
    sys.stdout.write("\n" + "=" * 60 + "\n")
    sys.stdout.write("PBIP SCHEMATHESIS FUZZING REPORT\n")
    sys.stdout.write("=" * 60 + "\n")
    sys.stdout.write(f"Target:                {res.target}\n")
    sys.stdout.write(f"OpenAPI Spec:          {res.spec_path}\n")
    sys.stdout.write(f"Operations Fuzzed:     {res.operations_count}\n")
    sys.stdout.write(f"Total Cases Executed:  {res.total_cases}\n")
    sys.stdout.write(f"Passed Cases:          {res.passed_cases}\n")
    sys.stdout.write(f"Unhandled 500 Errors:  {res.server_errors}\n")
    sys.stdout.write(f"Schema Violations:     {res.schema_violations}\n")
    sys.stdout.write(f"Execution Duration:    {res.duration_seconds}s\n")
    if res.duration_seconds > 0:
        throughput = round(res.total_cases / res.duration_seconds, 1)
        sys.stdout.write(f"Throughput:            {throughput} cases/sec\n")

    sys.stdout.write("\nStatus Code Distribution:\n")
    for code, count in res.status_codes.items():
        sys.stdout.write(f"  HTTP {code}: {count}\n")

    if res.failures:
        sys.stdout.write("\nDetected Failures:\n")
        for idx, fail in enumerate(res.failures[:10], start=1):
            sys.stdout.write(
                f"  [{idx}] {fail['error_type']} at {fail['method']} {fail['path']} "
                f"(Status: {fail['status_code']})\n"
                f"      Details: {fail['message'][:100]}...\n"
            )
        if len(res.failures) > 10:
            sys.stdout.write(f"  ... and {len(res.failures) - 10} more failures.\n")

    sys.stdout.write("=" * 60 + "\n")
    if res.is_success:
        sys.stdout.write("SUCCESS: Zero 500 errors and 100% schema conformance achieved.\n")
    else:
        sys.stdout.write("FAILURE: Fuzzing criteria violated.\n")
    sys.stdout.write("=" * 60 + "\n\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="PBIP Schemathesis Property-Based Fuzzer")
    parser.add_argument(
        "--cases",
        type=int,
        default=500,
        help="Target number of test cases to execute (default: 500, DoD requirement)",
    )
    parser.add_argument(
        "--url",
        type=str,
        default=None,
        help="Base URL of running PBIP API server. Runs in-process if omitted.",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="Bearer token for authenticated endpoint fuzzing.",
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=SPEC_DEFAULT,
        help="Path to OpenAPI JSON specification.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Hypothesis random seed for deterministic reproduction.",
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        default=None,
        help="Path to write JSON test report.",
    )
    args = parser.parse_args()

    result = run_fuzz(
        spec_path=args.spec,
        base_url=args.url,
        min_cases=args.cases,
        bearer_token=args.token,
        seed=args.seed,
    )

    print_summary(result)

    if args.report_file:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
        sys.stdout.write(f"Wrote report to {args.report_file}\n")

    if not result.is_success or result.total_cases < args.cases:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
