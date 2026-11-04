"""Repeatable latency measurement of clickstream ingestion (TASK-055, issue #78: < 15 ms).

Two separate measurements, because the criterion mixes them:

``http``  Logs in once (the login limit is 10/min per IP, so the token is reused), then sends
          ``--warmup`` and ``--requests`` sequential ``POST /api/v1/events/batch`` for each batch
          size and prints the status codes, median, p95, p99 and max. Client-side wall clock, so
          TLS, HTTP and JSON overhead are included: an upper bound on the server's time.
          Sequential only; throughput is ``infra/k6/events.js``. The default events limit is
          120/min per IP: run against a stack with a raised limit, or pass ``--pace``.

``db``    The write path without HTTP: the repository's insert into the partitioned
          ``clickstream_events`` table and the commit, timed separately per batch, through the API's
          own engine (``DATABASE_URL``). Prints per-batch and per-event cost for each batch size.
          It writes rows: point it at a scratch database, never the demo database.

Usage:
    uv run python scripts/measure_events_latency.py http --base-url http://127.0.0.1:8077 \\
        --email bob@demo.pbip --requests 500 [--ca infra/certs/ca.crt] [--json out.json]
    DATABASE_URL=... uv run python scripts/measure_events_latency.py db --email bob@demo.pbip \\
        --batches 300 [--json out.json]
The password is read from PBIP_LATENCY_PASSWORD, else the local constant of scripts/seed_load.py
(``load*@perf.pbip``) or scripts/seed_dev.py (the demo users). It is never printed.
"""

import argparse
import json
import os
import platform
import statistics
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx

# The API accepts at most MAX_BATCH_EVENTS = 25 (ADR-0055). The sizes above it that chose the cap
# (40, 50, 75, 100) were measured before it was lowered; ``db`` can still time them (no API limit).
DEFAULT_SIZES = (1, 10, 25)
API_MAX_BATCH = 25  # pbip_api.modules.events.schemas.MAX_BATCH_EVENTS; a unit test keeps them equal
DB_MAX_BATCH = 100


def percentile(samples: list[float], pct: float) -> float:
    """Nearest-rank percentile of a non-empty list (pct in 0..100)."""
    if not samples:
        msg = "no samples"
        raise ValueError(msg)
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil(n * pct / 100), at least 1
    return ordered[int(rank) - 1]


def summarise(samples: list[float]) -> dict[str, float]:
    return {
        "n": float(len(samples)),
        "median_ms": round(statistics.median(samples), 2),
        "p95_ms": round(percentile(samples, 95), 2),
        "p99_ms": round(percentile(samples, 99), 2),
        "max_ms": round(max(samples), 2),
    }


def _password(email: str) -> str:
    if os.environ.get("PBIP_LATENCY_PASSWORD"):
        return os.environ["PBIP_LATENCY_PASSWORD"]
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if email.endswith("@perf.pbip"):  # the load users from scripts/seed_load.py (Performance CI)
        from seed_load import LOAD_PASSWORD  # type: ignore[import-not-found]  # a sibling script

        return str(LOAD_PASSWORD)
    from seed_dev import DEMO_PASSWORD  # type: ignore[import-not-found]  # a sibling script

    return str(DEMO_PASSWORD)


def _load_average() -> list[float] | None:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except OSError:
        return None


def make_events(size: int, session_id: str) -> list[dict[str, object]]:
    """``size`` valid events shaped like the web tracker's, with fresh ids (none is a duplicate)."""
    now = datetime.now(UTC).isoformat()
    return [
        {
            "event_id": str(uuid.uuid4()),
            "event_type": "page_view",
            "event_timestamp": now,
            "session_id": session_id,
            "page_url": "/accounts/:id",
            "properties": {"position": index, "source": "benchmark"},
        }
        for index in range(size)
    ]


def _timed(call: Callable[[], Any]) -> tuple[float, Any]:
    started = time.perf_counter()
    result = call()
    return (time.perf_counter() - started) * 1000, result


# --- http -------------------------------------------------------------------------------------


def require_stored(status: int, body: object, sent: int) -> None:
    """Stop the measurement when a 202 did not store the batch.

    A batch the API discards (analytics consent off, ADR-0057) or skips as duplicates is answered
    much faster than an insert, so timing it would report the wrong path as the insert latency.
    """
    if status != 202:
        return  # counted in "statuses"; rate limiting and errors are reported, not hidden
    accepted = body.get("accepted") if isinstance(body, dict) else None
    if accepted != sent:
        msg = (
            f"a 202 stored {accepted} of {sent} events (not_recorded: "
            f"{body.get('not_recorded') if isinstance(body, dict) else None}); is the customer's "
            "analytics consent on? The insert path was not measured."
        )
        raise SystemExit(msg)


def http_size(
    client: httpx.Client, headers: dict[str, str], size: int, args: argparse.Namespace
) -> dict[str, object]:
    session_id = f"bench-{uuid.uuid4().hex[:12]}"
    statuses: Counter[int] = Counter()
    samples: list[float] = []
    for index in range(args.warmup + args.requests):
        body = {"events": make_events(size, session_id)}
        elapsed, response = _timed(
            partial(client.post, "/api/v1/events/batch", json=body, headers=headers)
        )
        require_stored(
            response.status_code,
            response.json() if response.status_code == 202 else None,
            len(body["events"]),
        )
        if index >= args.warmup:
            samples.append(elapsed)
            statuses[response.status_code] += 1
        if args.pace:
            time.sleep(args.pace)
    return {"statuses": dict(statuses), **summarise(samples)}


def run_http(args: argparse.Namespace) -> dict[str, object]:
    verify: str | bool = args.ca if args.ca else True
    with httpx.Client(base_url=args.base_url, timeout=30, verify=verify) as client:
        login = client.post(
            "/api/v1/auth/login", json={"email": args.email, "password": _password(args.email)}
        )
        login.raise_for_status()
        token = login.json().get("access_token")
        if not token:
            msg = "login did not return an access token (MFA user?); use a customer without MFA"
            raise SystemExit(msg)
        headers = {"Authorization": f"Bearer {token}"}
        # Events are stored only with the customer's analytics consent (ADR-0057); without it the
        # batch is discarded unstored and the insert this script measures would never run.
        consent = client.put(
            "/api/v1/customers/me/analytics-consent", json={"granted": True}, headers=headers
        )
        consent.raise_for_status()
        return {f"batch {size}": http_size(client, headers, size, args) for size in args.sizes}


# --- db ---------------------------------------------------------------------------------------


def db_size(
    session: Any, customer_id: Any, size: int, args: argparse.Namespace
) -> dict[str, object]:
    from pbip_api.modules.events import repository

    insert_ms: list[float] = []
    commit_ms: list[float] = []
    session_id = f"bench-db-{uuid.uuid4().hex[:12]}"
    for index in range(args.warmup + args.batches):
        now = datetime.now(UTC)
        records = [
            {
                "id": uuid.uuid4(),
                "event_timestamp": now,
                "event_type": "page_view",
                "customer_id": customer_id,
                "session_id": session_id,
                "page_url": "/accounts/:id",
                "properties": {"position": position, "source": "benchmark"},
                "created_at": now,
            }
            for position in range(size)
        ]
        elapsed_insert, inserted = _timed(partial(repository.insert_events_batch, session, records))
        elapsed_commit, _ = _timed(session.commit)
        if inserted != size:
            msg = f"expected {size} rows inserted, got {inserted}"
            raise SystemExit(msg)
        if index >= args.warmup:
            insert_ms.append(elapsed_insert)
            commit_ms.append(elapsed_commit)
    total = [a + b for a, b in zip(insert_ms, commit_ms, strict=True)]
    per_event_us = round(statistics.median(insert_ms) * 1000 / size, 1)
    return {
        "insert": summarise(insert_ms),
        "commit": summarise(commit_ms),
        "insert_plus_commit": summarise(total),
        "insert_per_event_us_median": per_event_us,
    }


def run_db(args: argparse.Namespace) -> dict[str, object]:
    from pbip_api.core.db import get_engine
    from pbip_api.modules.auth.models import User
    from pbip_api.modules.customers.models import Customer
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    with Session(get_engine(), expire_on_commit=False) as session:
        customer_id = session.execute(
            select(Customer.id)
            .join(User, Customer.user_id == User.id)
            .where(User.email == args.email)
        ).scalar_one()
        return {f"batch {size}": db_size(session, customer_id, size, args) for size in args.sizes}


# --- report -----------------------------------------------------------------------------------


def _row(name: str, r: dict[str, Any]) -> str:
    cells = " ".join(f"{float(r[key]):8.2f}" for key in ("median_ms", "p95_ms", "p99_ms", "max_ms"))
    return f"  {name:24s} {cells}\n"


def write_text(mode: str, results: dict[str, Any]) -> None:
    out = sys.stdout
    out.write("  measurement                median      p95      p99      max  (ms)\n")
    for name, r in results.items():
        if mode == "http":
            out.write(_row(f"{name} {r['statuses']}", r))
            continue
        for part in ("insert", "commit", "insert_plus_commit"):
            out.write(_row(f"{name} {part}", r[part]))
        out.write(f"  {name} insert per event (median): {r['insert_per_event_us_median']} us\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Clickstream ingestion latency (sequential)")
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("http", "db"):
        p = sub.add_parser(mode)
        p.add_argument("--email", required=True, help="A seeded customer without MFA")
        p.add_argument("--sizes", type=int, nargs="+", default=list(DEFAULT_SIZES))
        p.add_argument("--warmup", type=int, default=20)
        p.add_argument("--json", type=Path, default=None)
    http_parser = sub.choices["http"]
    http_parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    http_parser.add_argument("--ca", default=None, help="CA bundle, e.g. infra/certs/ca.crt")
    http_parser.add_argument("--requests", type=int, default=500)
    http_parser.add_argument("--pace", type=float, default=0.0, help="Seconds between requests")
    sub.choices["db"].add_argument("--batches", type=int, default=300)
    args = parser.parse_args()
    limit = API_MAX_BATCH if args.mode == "http" else DB_MAX_BATCH
    if any(not 1 <= size <= limit for size in args.sizes):
        parser.error(f"{args.mode} batch sizes must be 1-{limit}")

    load_before = _load_average()
    results = run_http(args) if args.mode == "http" else run_db(args)
    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "mode": args.mode,
        "base_url": getattr(args, "base_url", None),
        "samples_per_size": getattr(args, "requests", None) or args.batches,
        "warmup_per_size": args.warmup,
        "environment": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
            "load_average_before": load_before,
            "load_average_after": _load_average(),
        },
        "results": results,
    }
    sys.stdout.write(
        f"{args.mode}  n={report['samples_per_size']} per size, {args.warmup} warm-up, sequential, "
        f"load average {load_before} -> {report['environment']['load_average_after']}\n"
    )
    write_text(args.mode, results)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
