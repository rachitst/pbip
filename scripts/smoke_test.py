#!/usr/bin/env python3
"""Cross-platform post-deploy smoke test for PBIP.

Replaces bash/curl/seq dependency while maintaining identical checks.
Usage:
    python scripts/smoke_test.py http://localhost:8080 [--api-only]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | bytes | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    """Perform HTTP request returning (status_code, headers_lower, body)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        msg = f"Unsupported URL scheme: {parsed.scheme}"
        raise ValueError(msg)

    req_headers = headers.copy() if headers else {}
    body_bytes: bytes | None = None
    if isinstance(data, dict):
        body_bytes = json.dumps(data).encode("utf-8")
        if "Content-Type" not in req_headers:
            req_headers["Content-Type"] = "application/json"
    elif isinstance(data, bytes):
        body_bytes = data

    req = urllib.request.Request(  # noqa: S310
        url, data=body_bytes, headers=req_headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            resp_headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, resp_headers, response.read()
    except urllib.error.HTTPError as err:
        resp_headers = {k.lower(): v for k, v in err.headers.items()}
        return err.code, resp_headers, err.read()
    except Exception as exc:
        return 0, {}, str(exc).encode("utf-8")


def wait_for_readiness(base_url: str, max_retries: int = 45, interval: float = 2.0) -> bool:
    """Poll readiness endpoint until HTTP 200 or timeout."""
    ready_url = f"{base_url}/api/health/ready"
    sys.stdout.write("Waiting for readiness (up to 90s)...\n")
    for _ in range(max_retries):
        status, _, _ = http_request(ready_url, timeout=5.0)
        if status == 200:
            return True
        time.sleep(interval)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="PBIP post-deploy smoke test")
    parser.add_argument("base_url", help="Base URL to test against (e.g. http://localhost:8080)")
    parser.add_argument(
        "--api-only", action="store_true", help="Only verify API endpoints, skip static web checks"
    )
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    failures = 0

    def check(name: str, expected_status: int, actual_status: int) -> None:
        nonlocal failures
        if actual_status == expected_status:
            sys.stdout.write(f"  ok   {name} ({actual_status})\n")
        else:
            sys.stdout.write(f"  FAIL {name}: expected {expected_status}, got {actual_status}\n")
            failures += 1

    if not wait_for_readiness(base):
        sys.stdout.write("  FAIL readiness check timed out\n")
        failures += 1

    sys.stdout.write(f"Smoke test against {base}\n")
    status, _, _ = http_request(f"{base}/api/health/ready")
    check("readiness", 200, status)

    status, _, _ = http_request(f"{base}/api/v1/accounts")
    check("unauthenticated /accounts", 401, status)

    status, _, _ = http_request(
        f"{base}/api/v1/auth/login",
        method="POST",
        data={"email": "nobody@example.com", "password": "definitely-wrong-123"},
    )
    check("bad credentials rejected", 401, status)

    status, _, _ = http_request(
        f"{base}/api/v1/transfers",
        method="POST",
        data={},
    )
    check("transfer without auth", 401, status)

    _, live_headers, _ = http_request(f"{base}/api/health/live")
    if live_headers.get("x-content-type-options", "").lower() == "nosniff":
        sys.stdout.write("  ok   API security headers\n")
    else:
        sys.stdout.write(
            "  FAIL API security headers: x-content-type-options missing or incorrect\n"
        )
        failures += 1

    if not args.api_only:
        status, _, body = http_request(f"{base}/")
        check("web app served", 200, status)

        status, _, _ = http_request(f"{base}/metrics")
        check("metrics not exposed publicly", 404, status)

        if b"PBIP" in body:
            sys.stdout.write("  ok   web page content\n")
        else:
            sys.stdout.write("  FAIL web page content: missing 'PBIP'\n")
            failures += 1

    if failures > 0:
        sys.stdout.write(f"SMOKE TEST FAILED ({failures})\n")
        return 1

    sys.stdout.write("SMOKE TEST PASSED\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
