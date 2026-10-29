#!/usr/bin/env python3
"""Property-based API fuzzing of the PBIP OpenAPI contract with Schemathesis (TASK-070).

Every operation in ``docs/api/openapi.json`` is driven with Hypothesis-generated requests in two
modes: *positive* (schema-valid payloads, which reach the service layer) and *negative* (payloads
that violate the schema, which must be rejected with 4xx). Two checks apply to every response:
``not_a_server_error`` (no 5xx, no transport crash) and ``response_schema_conformance`` (the body
matches the schema declared for that status code).

Why authentication is built in: without a bearer token every protected route answers 401 before
any validation runs, so a run would prove nothing beyond "the auth guard is first". Unless
``--no-auth`` is given, the runner registers a disposable customer through the API under test and
fuzzes with that customer's token. Staff routes (roles + MFA) are therefore exercised only on
their authorization-denied path; ``--token`` can supply a staff token to go further.

Targets: a live server (``--url``) or the in-process app, which needs a migrated PostgreSQL
(``--database-url``, default ``TEST_DATABASE_URL``/``DATABASE_URL``) exactly like ``make test-int``.
``--seed`` makes the generated inputs reproducible; the report carries a digest of them.

Usage:
    python scripts/fuzz.py --url http://127.0.0.1:8000 --cases 500 --report-file fuzz-report.json
    python scripts/dev.py fuzz --cases 500 --seed 7
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import secrets
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[1]
SPEC_DEFAULT = ROOT / "docs" / "api" / "openapi.json"
REGISTER_PATH = "/api/v1/auth/register"
LOGIN_PATH = "/api/v1/auth/login"
ME_PATH = "/api/v1/auth/me"
GENERATION_MODES = ("positive", "negative")
# Upper bound on passes over the operation list when topping up to --cases (Hypothesis runs an
# operation without any input only once per pass).
MAX_CYCLES = 25
PROGRESS_EVERY = 100

for extra_path in (ROOT / "apps" / "api" / "src", ROOT / "apps" / "api" / "tests"):
    if str(extra_path) not in sys.path:
        sys.path.insert(0, str(extra_path))

if TYPE_CHECKING:
    from fastapi import FastAPI
    from schemathesis.generation.case import Case
    from schemathesis.schemas import APIOperation

# JSON payloads have no narrower static type than Any.
Json = dict[str, Any]


@dataclass
class FuzzFailure:
    """One request whose response violated a check (or could not be completed at all)."""

    method: str
    path: str
    status_code: int
    error_type: str
    message: str
    request_data: Json | None = None


@dataclass
class FuzzResult:
    """Aggregated outcome of a run; ``failures`` carries the reproducible details."""

    spec_path: str
    target: str
    authenticated: bool
    generation_modes: list[str]
    seed: int | None
    total_cases: int
    operations_count: int
    passed_cases: int
    server_errors: int
    schema_violations: int
    status_codes: dict[str, int]
    per_operation: dict[str, dict[str, int]]
    unsatisfiable: list[str]
    input_digest: str
    duration_seconds: float
    failures: list[Json]

    @property
    def is_success(self) -> bool:
        return self.server_errors == 0 and self.schema_violations == 0 and not self.failures


class Target:
    """Where requests go: a live base URL or an in-process ASGI app."""

    def __init__(self, base_url: str | None = None, app: FastAPI | None = None) -> None:
        if (base_url is None) == (app is None):
            msg = "exactly one of base_url or app is required"
            raise ValueError(msg)
        self.base_url = base_url.rstrip("/") if base_url else None
        self.app = app
        self.description = self.base_url or "in-process ASGI app"

    def call(self, case: Case, headers: dict[str, str]) -> Any:
        if self.base_url:
            return case.call(base_url=self.base_url, headers=headers or None)
        return case.call(app=self.app, headers=headers or None)

    def post_json(self, path: str, payload: Json) -> tuple[int, Json]:
        if self.base_url:
            import requests

            live = requests.post(f"{self.base_url}{path}", json=payload, timeout=30)
            return live.status_code, dict(live.json()) if live.content else {}
        from fastapi.testclient import TestClient

        local = TestClient(self.app, raise_server_exceptions=False).post(path, json=payload)
        return local.status_code, dict(local.json()) if local.content else {}

    def get_status(self, path: str, headers: dict[str, str]) -> int:
        if self.base_url:
            import requests

            return requests.get(f"{self.base_url}{path}", headers=headers, timeout=30).status_code
        from fastapi.testclient import TestClient

        client = TestClient(self.app, raise_server_exceptions=False)
        return client.get(path, headers=headers).status_code


class FuzzIdentity:
    """A disposable customer registered through the API under test (to pass the 401 guard)."""

    def __init__(self, target: Target) -> None:
        self.target = target
        self.email = f"fuzz-{secrets.token_hex(6)}@example.com"
        self.password = f"Fuzz-Horse-{secrets.token_hex(4)}!9"
        self.token = ""

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def provision(self) -> dict[str, str]:
        payload = {"email": self.email, "password": self.password, "full_name": "Fuzz Customer"}
        status, _ = self.target.post_json(REGISTER_PATH, payload)
        if status != 201:
            msg = f"could not register the fuzz customer ({self.target.description}): HTTP {status}"
            raise RuntimeError(msg)
        return self.login()

    def login(self) -> dict[str, str]:
        status, body = self.target.post_json(
            LOGIN_PATH, {"email": self.email, "password": self.password}
        )
        if status != 200 or "access_token" not in body:
            msg = f"could not log the fuzz customer in on {self.target.description}: HTTP {status}"
            raise RuntimeError(msg)
        self.token = str(body["access_token"])
        return self.headers

    def ensure(self) -> dict[str, str]:
        """Log in again if a fuzzed operation managed to invalidate the session."""
        if self.target.get_status(ME_PATH, self.headers) == 401:
            return self.login()
        return self.headers


def build_isolated_app(database_url: str | None = None) -> FastAPI:
    """The real application on a real (migrated) database, with Redis and secrets replaced."""
    import fakeredis
    from cryptography.fernet import Fernet
    from pbip_api.core.config import Settings, get_settings
    from pbip_api.core.redis import get_redis
    from pbip_api.main import create_app

    overrides = {"database_url": database_url} if database_url else {}
    settings = Settings(
        app_env="local",
        field_encryption_key=Fernet.generate_key().decode(),
        log_level="ERROR",
        **overrides,
    )
    redis = fakeredis.FakeRedis(decode_responses=True)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_redis] = lambda: redis
    return app


def _request_data(case: Case) -> Json:
    raw_headers = dict(case.headers or {})
    return {
        "path_parameters": case.path_parameters,
        "query": case.query,
        "headers": {k: v for k, v in raw_headers.items() if k.lower() != "authorization"},
        "body": case.body,
    }


Executor = Callable[["Case", dict[str, str]], tuple[int, FuzzFailure | None]]
# Schemathesis reports failed checks as a FailureGroup, which derives from BaseExceptionGroup, not
# Exception: catching only Exception let a real 5xx escape and abort a whole nightly run.
_CAUGHT = (Exception, BaseExceptionGroup)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(f"{type(e).__name__}: {e}" for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def _make_executor(target: Target) -> Executor:
    def execute(case: Case, headers: dict[str, str]) -> tuple[int, FuzzFailure | None]:
        from schemathesis.checks import not_a_server_error, response_schema_conformance

        try:
            response = target.call(case, headers)
        except _CAUGHT as exc:  # a transport or ASGI crash is a finding, not a runner bug
            failure = FuzzFailure(
                method=case.method,
                path=case.path,
                status_code=0,
                error_type="NetworkOrTransportError",
                message=_describe(exc),
                request_data=_request_data(case),
            )
            return 0, failure
        status = int(response.status_code)
        try:
            case.validate_response(
                response, checks=[not_a_server_error, response_schema_conformance]
            )
        except _CAUGHT as exc:  # one FailureGroup per failed check; the status says which
            error_type = "UnhandledServerError" if status >= 500 else "ResponseSchemaViolation"
            failure = FuzzFailure(
                method=case.method,
                path=case.path,
                status_code=status,
                error_type=error_type,
                message=_describe(exc),
                request_data=_request_data(case),
            )
            return status, failure
        return status, None

    return execute


class _Tally:
    """Counts outcomes and hashes every generated input (for reproducibility checks)."""

    def __init__(self, goal: int) -> None:
        self.goal = goal
        self.executed = 0
        self.server_errors = 0
        self.schema_violations = 0
        self.status_counts: Counter[str] = Counter()
        self.per_operation: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self.failures: list[FuzzFailure] = []
        self._digest = hashlib.sha256()

    def record(self, case: Case, status: int, failure: FuzzFailure | None) -> None:
        self.executed += 1
        self._digest.update(
            json.dumps(
                _request_data(case) | {"method": case.method}, default=str, sort_keys=True
            ).encode()
        )
        if status > 0:
            self.status_counts[str(status)] += 1
        self.per_operation[f"{case.method.upper()} {case.path}"][str(status)] += 1
        if failure is not None:
            self.failures.append(failure)
            if failure.status_code >= 500:
                self.server_errors += 1
            elif failure.error_type == "ResponseSchemaViolation":
                self.schema_violations += 1
        if self.executed % PROGRESS_EVERY == 0 or self.executed == self.goal:
            sys.stdout.write(
                f"Progress: {self.executed}/{self.goal} cases "
                f"({self.server_errors} 5xx, {self.schema_violations} schema violations)\n"
            )
            sys.stdout.flush()

    @property
    def input_digest(self) -> str:
        return self._digest.hexdigest()


def _load_operations(spec_path: Path) -> list[APIOperation]:
    import schemathesis
    from schemathesis.core.result import Ok

    if not spec_path.exists():
        msg = f"OpenAPI specification not found: {spec_path}"
        raise FileNotFoundError(msg)
    schema = schemathesis.openapi.from_path(str(spec_path))
    operations = [item.ok() for item in schema.get_all_operations() if isinstance(item, Ok)]
    if not operations:
        msg = f"No valid OpenAPI operations found in {spec_path}"
        raise RuntimeError(msg)
    return sorted(operations, key=lambda op: (op.path, op.method))


def _operation_seed(seed: int, cycle: int, index: int, mode: str) -> int:
    material = f"{seed}:{cycle}:{index}:{mode}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _generate(
    operation: APIOperation,
    mode: str,
    examples: int,
    seed: int | None,
    consume: Callable[[Case], None],
) -> str | None:
    """Feed up to ``examples`` generated cases to ``consume``; returns why if none could be."""
    from hypothesis import HealthCheck, Phase, given
    from hypothesis import seed as hypothesis_seed
    from hypothesis import settings as hypothesis_settings
    from hypothesis.errors import Unsatisfiable
    from schemathesis.generation import GenerationMode

    def body(case: Case) -> None:
        consume(case)

    strategy = operation.as_strategy(generation_mode=GenerationMode(mode))
    test = hypothesis_settings(
        max_examples=examples,
        deadline=None,
        database=None,
        phases=[Phase.generate],
        suppress_health_check=list(HealthCheck),
    )(given(strategy)(body))
    if seed is not None:
        test = hypothesis_seed(seed)(test)
    try:
        test()
    except Unsatisfiable:
        return "no input of this kind can be generated"
    return None


def _authenticate(
    target: Target, auth: bool, bearer_token: str | None
) -> tuple[FuzzIdentity | None, dict[str, str]]:
    if bearer_token:
        return None, {"Authorization": f"Bearer {bearer_token}"}
    if auth:
        identity = FuzzIdentity(target)
        return identity, identity.provision()
    return None, {}


def _fuzz_operations(
    operations: list[APIOperation],
    modes: tuple[str, ...],
    min_cases: int,
    seed: int | None,
    executor: Executor,
    identity: FuzzIdentity | None,
    headers: dict[str, str],
) -> tuple[_Tally, dict[str, str]]:
    """Full passes over every operation and mode until at least ``min_cases`` were checked.

    A pass is never cut short: "across all endpoints" is part of the acceptance criteria, so the
    case budget is a floor and every operation is fuzzed in every pass.
    """
    tally = _Tally(min_cases)
    per_round = max(1, math.ceil(min_cases / (len(operations) * len(modes))))
    unsatisfiable: dict[str, str] = {}
    cycle = 0
    while tally.executed < min_cases and cycle < MAX_CYCLES:
        for index, operation in enumerate(operations):
            if identity is not None:
                headers = identity.ensure()
            for mode in modes:
                label = f"{mode} {operation.method.upper()} {operation.path}"
                if label in unsatisfiable:
                    continue

                def consume(case: Case, headers: dict[str, str] = headers) -> None:
                    status, failure = executor(case, headers)
                    tally.record(case, status, failure)

                op_seed = None if seed is None else _operation_seed(seed, cycle, index, mode)
                reason = _generate(operation, mode, per_round, op_seed, consume)
                if reason is not None:
                    unsatisfiable[label] = reason
        cycle += 1
    return tally, unsatisfiable


def run_fuzz(
    spec_path: Path = SPEC_DEFAULT,
    base_url: str | None = None,
    app: FastAPI | None = None,
    min_cases: int = 500,
    auth: bool = True,
    bearer_token: str | None = None,
    seed: int | None = None,
    modes: tuple[str, ...] = GENERATION_MODES,
    database_url: str | None = None,
    execute: Executor | None = None,
) -> FuzzResult:
    """Fuzz every operation until at least ``min_cases`` requests were checked."""
    logging.getLogger("pbip_api").setLevel(logging.ERROR)
    logging.getLogger("schemathesis").setLevel(logging.WARNING)

    operations = _load_operations(spec_path)
    if base_url is None and app is None:
        app = build_isolated_app(database_url)
    target = Target(base_url=base_url, app=app)
    executor = execute or _make_executor(target)

    identity, headers = _authenticate(target, auth, bearer_token)

    sys.stdout.write(f"Loaded {len(operations)} operations from {spec_path.relative_to(ROOT)}\n")
    sys.stdout.write(
        f"Fuzzing target: {target.description} (modes: {', '.join(modes)}; "
        f"authenticated: {bool(headers)}; target cases: {min_cases})\n"
    )
    sys.stdout.flush()

    started = time.perf_counter()
    tally, unsatisfiable = _fuzz_operations(
        operations, modes, min_cases, seed, executor, identity, headers
    )

    return FuzzResult(
        spec_path=str(spec_path.relative_to(ROOT)),
        target=target.description,
        authenticated=bool(headers),
        generation_modes=list(modes),
        seed=seed,
        total_cases=tally.executed,
        operations_count=len(operations),
        passed_cases=tally.executed - len(tally.failures),
        server_errors=tally.server_errors,
        schema_violations=tally.schema_violations,
        status_codes=dict(sorted(tally.status_counts.items())),
        per_operation={k: dict(sorted(v.items())) for k, v in sorted(tally.per_operation.items())},
        unsatisfiable=sorted(unsatisfiable),
        input_digest=tally.input_digest,
        duration_seconds=round(time.perf_counter() - started, 2),
        failures=[asdict(f) for f in tally.failures],
    )


def print_summary(res: FuzzResult) -> None:
    """Human-readable report; the JSON report (``--report-file``) has the full detail."""
    line = "=" * 60 + "\n"
    out = sys.stdout
    out.write("\n" + line + "PBIP SCHEMATHESIS FUZZING REPORT\n" + line)
    out.write(f"Target:                {res.target}\n")
    out.write(f"OpenAPI Spec:          {res.spec_path}\n")
    out.write(f"Authenticated:         {res.authenticated}\n")
    out.write(f"Generation modes:      {', '.join(res.generation_modes)}\n")
    out.write(f"Seed:                  {res.seed}\n")
    out.write(f"Operations Fuzzed:     {res.operations_count}\n")
    out.write(f"Total Cases Executed:  {res.total_cases}\n")
    out.write(f"Passed Cases:          {res.passed_cases}\n")
    out.write(f"Unhandled 5xx Errors:  {res.server_errors}\n")
    out.write(f"Schema Violations:     {res.schema_violations}\n")
    out.write(f"Execution Duration:    {res.duration_seconds}s\n")
    out.write(f"Input digest:          {res.input_digest[:16]} (same --seed => same inputs)\n")
    if res.duration_seconds > 0:
        rate = round(res.total_cases / res.duration_seconds, 1)
        out.write(f"Throughput:            {rate} cases/sec\n")

    out.write("\nStatus Code Distribution:\n")
    for code, count in res.status_codes.items():
        out.write(f"  HTTP {code}: {count}\n")
    out.write("\nPer Operation:\n")
    for name, codes in res.per_operation.items():
        out.write(f"  {name}: {', '.join(f'{k} x{v}' for k, v in codes.items())}\n")
    if res.unsatisfiable:
        out.write("\nNot generated (nothing to fuzz in this mode):\n")
        for label in res.unsatisfiable:
            out.write(f"  {label}\n")
    if res.failures:
        out.write("\nDetected Failures:\n")
        for idx, fail in enumerate(res.failures[:10], start=1):
            out.write(
                f"  [{idx}] {fail['error_type']} at {fail['method']} {fail['path']} "
                f"(Status: {fail['status_code']})\n      Details: {fail['message'][:160]}\n"
            )
        if len(res.failures) > 10:
            out.write(f"  ... and {len(res.failures) - 10} more failures.\n")
    out.write(line)
    if res.is_success:
        out.write("SUCCESS: Zero 5xx errors and 100% schema conformance.\n")
    else:
        out.write("FAILURE: Fuzzing criteria violated.\n")
    out.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="PBIP Schemathesis property-based fuzzer")
    parser.add_argument("--cases", type=int, default=500, help="Minimum cases (DoD: 500)")
    parser.add_argument(
        "--url", default=None, help="Base URL of a running API; in-process when omitted"
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("TEST_DATABASE_URL"),
        help="Migrated PostgreSQL for the in-process target (default: TEST_DATABASE_URL)",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Do not register a fuzz customer; fuzz the unauthenticated surface only",
    )
    parser.add_argument(
        "--token", default=None, help="Use this bearer token instead of a registered customer"
    )
    parser.add_argument(
        "--mode",
        choices=(*GENERATION_MODES, "both"),
        default="both",
        help="positive (schema-valid), negative (schema-violating) or both",
    )
    parser.add_argument("--spec", type=Path, default=SPEC_DEFAULT, help="OpenAPI JSON path")
    parser.add_argument("--seed", type=int, default=None, help="Reproduce the generated inputs")
    parser.add_argument("--report-file", type=Path, default=None, help="Write a JSON report")
    args = parser.parse_args()

    modes = GENERATION_MODES if args.mode == "both" else (args.mode,)
    result = run_fuzz(
        spec_path=args.spec,
        base_url=args.url,
        min_cases=args.cases,
        auth=not args.no_auth,
        bearer_token=args.token,
        seed=args.seed,
        modes=modes,
        database_url=args.database_url,
    )
    print_summary(result)

    if args.report_file:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
        sys.stdout.write(f"Wrote report to {args.report_file}\n")

    return 0 if result.is_success and result.total_cases >= args.cases else 1


if __name__ == "__main__":
    raise SystemExit(main())
