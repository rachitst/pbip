"""Repeatable latency measurement of the analytics endpoints (TASK-040 target: < 50 ms).

Logs in as one customer, sends ``--warmup`` then ``--requests`` sequential GETs to each analytics
endpoint for one of their accounts, and prints sample count, median, p95, p99 and max per endpoint,
with the environment. Client-side wall clock, so HTTP and JSON overhead are included (an upper bound
on server time). Sequential only: this is a latency check, not a load test (TASK-078 / TASK-102).

Usage:
    uv run python scripts/measure_analytics_latency.py --base-url http://localhost:8000 \\
        --email alice@demo.pbip --requests 300 [--json results.json]
The password is read from PBIP_LATENCY_PASSWORD, or the demo password from scripts/seed_dev.py.
"""

import argparse
import json
import os
import platform
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ENDPOINTS = {
    "spending (running month)": "/api/v1/analytics/spending?account_id={account}",
    "spending (previous month)": (
        "/api/v1/analytics/spending?account_id={account}&year={py}&month={pm}"
    ),
    "trends": "/api/v1/analytics/trends?account_id={account}",
    "health-score": "/api/v1/analytics/health-score?account_id={account}",
    "segment": "/api/v1/analytics/segment",
    "recommendations": "/api/v1/recommendations",
}


def percentile(samples: list[float], pct: float) -> float:
    """Nearest-rank percentile of a non-empty list (pct in 0..100)."""
    if not samples:
        msg = "no samples"
        raise ValueError(msg)
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil(n * pct / 100), at least 1
    return ordered[int(rank) - 1]


def summarise(samples: list[float]) -> dict[str, float | int]:
    return {
        "n": len(samples),
        "median_ms": round(statistics.median(samples), 1),
        "p95_ms": round(percentile(samples, 95), 1),
        "p99_ms": round(percentile(samples, 99), 1),
        "max_ms": round(max(samples), 1),
    }


def _password() -> str:
    if os.environ.get("PBIP_LATENCY_PASSWORD"):
        return os.environ["PBIP_LATENCY_PASSWORD"]
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from seed_dev import DEMO_PASSWORD

    return DEMO_PASSWORD


def measure(
    client: httpx.Client, path: str, headers: dict[str, str], warmup: int, n: int
) -> dict[str, object]:
    for _ in range(warmup):
        client.get(path, headers=headers)
    samples: list[float] = []
    statuses: set[int] = set()
    for _ in range(n):
        started = time.perf_counter()
        response = client.get(path, headers=headers)
        samples.append((time.perf_counter() - started) * 1000)
        statuses.add(response.status_code)
    return {"statuses": ",".join(str(code) for code in sorted(statuses)), **summarise(samples)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Analytics endpoint latency (sequential)")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--account-id", default=None, help="Defaults to the customer's first account"
    )
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    now = datetime.now(UTC)
    py, pm = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        login = client.post(
            "/api/v1/auth/login", json={"email": args.email, "password": _password()}
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        account = args.account_id or client.get("/api/v1/accounts", headers=headers).json()[0]["id"]
        results = {
            name: measure(
                client,
                path.format(account=account, py=py, pm=pm),
                headers,
                args.warmup,
                args.requests,
            )
            for name, path in ENDPOINTS.items()
        }
    report = {
        "measured_at": now.isoformat(),
        "base_url": args.base_url,
        "requests_per_endpoint": args.requests,
        "warmup_per_endpoint": args.warmup,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
        },
        "results": results,
    }
    out = sys.stdout
    out.write(
        f"{args.base_url}  n={args.requests} per endpoint, {args.warmup} warm-up, sequential\n"
    )
    out.write("  endpoint                      status  median     p95     p99     max  (ms)\n")
    for name, r in results.items():
        cells = " ".join(
            f"{float(str(r[key])):7.1f}" for key in ("median_ms", "p95_ms", "p99_ms", "max_ms")
        )
        out.write(f"  {name:28s} {r['statuses']!s:>7s} {cells}\n")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
