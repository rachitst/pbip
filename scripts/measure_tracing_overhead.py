"""Repeatable measurement of OpenTelemetry tracing overhead (TASK-085 target: < 5 ms/request).

Runs the real API in-process (FastAPI TestClient, real PostgreSQL and Redis from the environment)
once per mode, each in a fresh interpreter because a process can set the tracer provider only
once. Every mode logs in as the same customer and sends ``--warmup`` then ``--requests``
sequential ``GET /api/v1/accounts`` (authentication, the policy check and SQL), then prints sample
count, median, p95 and p99 per mode and the median/p95 deltas against tracing off.

Modes: ``off`` (OTEL_ENABLED=false), ``none`` (spans created, trace ids in logs, nothing
exported), ``memory`` (spans also exported to the in-memory exporter). In-process timing excludes
the network, so the deltas isolate the instrumentation cost.

Usage (stack running, database migrated):
    uv run python scripts/measure_tracing_overhead.py --requests 500 [--json results.json]
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_analytics_latency import summarise

MODES: dict[str, dict[str, str]] = {
    "off": {"OTEL_ENABLED": "false"},
    "none": {"OTEL_ENABLED": "true", "OTEL_EXPORTER": "none"},
    "memory": {"OTEL_ENABLED": "true", "OTEL_EXPORTER": "memory"},
}


def _client() -> Any:
    import logging

    from fastapi.testclient import TestClient
    from pbip_api.main import create_app

    client = TestClient(create_app())
    logging.disable(logging.CRITICAL)  # access logs to stdout would add I/O to every sample
    return client


def _register(email: str, password: str) -> None:
    response = _client().post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Overhead Probe"},
    )
    response.raise_for_status()


def _measure(email: str, password: str, warmup: int, requests: int) -> list[float]:
    import structlog

    client = _client()
    token = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    token.raise_for_status()
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    structlog.configure(logger_factory=structlog.ReturnLoggerFactory())  # no stdout I/O
    for _ in range(warmup):
        client.get("/api/v1/accounts", headers=headers).raise_for_status()
    samples: list[float] = []
    for _ in range(requests):
        started = time.perf_counter()
        response = client.get("/api/v1/accounts", headers=headers)
        samples.append((time.perf_counter() - started) * 1000)
        response.raise_for_status()
    return samples


def compute_deltas(
    results: dict[str, dict[str, float | int]],
) -> dict[str, dict[str, float]]:
    """Median and p95 of every traced mode minus tracing ``off`` (ms)."""
    base = results["off"]
    return {
        mode: {
            key: round(float(summary[f"{key}_ms"]) - float(base[f"{key}_ms"]), 1)
            for key in ("median", "p95")
        }
        for mode, summary in results.items()
        if mode != "off"
    }


def format_lines(
    results: dict[str, dict[str, float | int]], deltas: dict[str, dict[str, float]]
) -> str:
    lines = [
        f"{mode:7s} n={r['n']} median={r['median_ms']} ms p95={r['p95_ms']} ms p99={r['p99_ms']} ms"
        for mode, r in results.items()
    ]
    lines += [
        f"delta {mode:7s} median={d['median']:+.1f} ms p95={d['p95']:+.1f} ms"
        for mode, d in deltas.items()
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--child", choices=sorted(MODES), help=argparse.SUPPRESS)
    parser.add_argument("--email", help=argparse.SUPPRESS)
    args = parser.parse_args()
    password = os.environ.get("PBIP_OVERHEAD_PASSWORD") or f"{uuid.uuid4()}Aa1!"

    if args.child:
        samples = _measure(args.email, password, args.warmup, args.requests)
        sys.stdout.write(json.dumps(samples) + "\n")
        return 0

    email = f"otel-overhead-{uuid.uuid4().hex[:8]}@example.com"
    _register(email, password)
    results: dict[str, dict[str, float | int]] = {}
    for mode, env in MODES.items():
        child = subprocess.run(
            [
                sys.executable,
                __file__,
                "--child",
                mode,
                "--email",
                email,
                "--requests",
                str(args.requests),
                "--warmup",
                str(args.warmup),
            ],
            env={**os.environ, **env, "PBIP_OVERHEAD_PASSWORD": password},
            capture_output=True,
            text=True,
            check=True,
        )
        results[mode] = summarise(json.loads(child.stdout.strip().splitlines()[-1]))

    deltas = compute_deltas(results)
    report = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "endpoint": "GET /api/v1/accounts (in-process, sequential)",
        "modes": results,
        "delta_ms": deltas,
    }
    sys.stdout.write(format_lines(results, deltas))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
