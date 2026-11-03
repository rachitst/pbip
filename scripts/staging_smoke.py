"""Smoke test of a deployed PBIP stack as a signed-in customer (#198, after scripts/smoke_test.py).

    PBIP_SMOKE_EMAIL=... PBIP_SMOKE_PASSWORD=... python3 scripts/staging_smoke.py https://<host>
    python3 scripts/staging_smoke.py https://<ip> --cacert pbip-staging-ca.crt   # private CA
    python3 scripts/staging_smoke.py https://<host> --check-http-redirect --no-login

``scripts/smoke_test.py`` (run by the deploy job) proves the anonymous surface. This adds what
only a real host can show:
  1. TLS: the certificate chain and host name verify (system trust store, or --cacert), the
     protocol is TLS 1.2 or newer and the certificate is valid for --min-cert-days more days;
  2. optional: http://<host>/ answers 301 to https://<host> (Let's Encrypt set-up, port 80 open);
  3. liveness and readiness are 200 and API responses carry HSTS;
  4. a customer signs in with a password (no authenticator), lists their accounts (read-only:
     200 and a JSON array) and signs out (204), so the session it created is revoked.

Credentials come only from the environment (never the command line, never printed); use a dedicated
smoke-test customer registered through the app. Only counts are printed: no e-mail address, token or
account number. Standard library only. Exit 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import http.client
import http.cookiejar
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

CSRF_HEADER = {"X-Requested-With": "pbip-web"}  # the API's csrf_guard on refresh/logout
TIMEOUT_S = 15.0


@dataclass(slots=True)
class Result:
    failures: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str) -> None:
        sys.stdout.write(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}\n")
        if not ok:
            self.failures.append(name)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The redirect itself is what step 2 checks, so it must not be followed."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def tls_context(cacert: str | None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=cacert)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def check_tls(result: Result, host: str, port: int, context: ssl.SSLContext, min_days: int) -> None:
    try:
        with (
            socket.create_connection((host, port), timeout=TIMEOUT_S) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            version = tls.version() or "unknown"
            cert = tls.getpeercert()
    except (OSError, ssl.SSLError) as exc:
        result.check("TLS handshake", False, f"{type(exc).__name__}: {exc}")
        return
    result.check("TLS handshake", True, f"{version}, chain and host name verified")
    not_after = cert.get("notAfter") if cert else None
    if not isinstance(not_after, str):
        result.check("certificate validity", False, "no notAfter in the peer certificate")
        return
    days = int((ssl.cert_time_to_seconds(not_after) - time.time()) // 86400)
    result.check("certificate validity", days >= min_days, f"{days} days left (minimum {min_days})")


def _lower(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {name.lower(): value for name, value in items}


def request(
    opener: urllib.request.OpenerDirector,
    url: str,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """(status, lower-cased response headers, body); status 0 when nothing answered."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))  # noqa: S310
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, timeout=TIMEOUT_S) as response:
            return response.status, _lower(response.headers.items()), response.read()
    except urllib.error.HTTPError as err:
        return err.code, _lower(err.headers.items()), err.read()
    except (OSError, http.client.HTTPException) as exc:
        return 0, {}, type(exc).__name__.encode()


def check_redirect(result: Result, host: str, http_port: int) -> None:
    opener = urllib.request.build_opener(_NoRedirect)
    status, headers, _ = request(opener, f"http://{host}:{http_port}/")
    location = headers.get("location", "")
    ok = status == 301 and location.startswith(f"https://{host}")
    result.check("HTTP redirects to HTTPS", ok, f"{status} -> {location or 'no Location'}")


def check_health(result: Result, opener: urllib.request.OpenerDirector, base: str) -> None:
    status, headers, _ = request(opener, f"{base}/api/health/live")
    result.check("liveness", status == 200, str(status))
    hsts = headers.get("strict-transport-security", "")
    result.check("HSTS on API responses", "max-age=" in hsts, hsts or "missing")
    status, _, _ = request(opener, f"{base}/api/health/ready")
    result.check("readiness", status == 200, str(status))


def _json(status: int, raw: bytes) -> Any:
    if status != 200:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _access_token(result: Result, status: int, raw: bytes) -> str | None:
    payload = _json(status, raw)
    if not isinstance(payload, dict):
        payload = {}
    if payload.get("mfa_required"):
        result.check("customer login", False, "this user has an authenticator; use one without")
        return None
    token = payload.get("access_token")
    result.check("customer login", isinstance(token, str), str(status))
    return token if isinstance(token, str) else None


def check_customer(
    result: Result, opener: urllib.request.OpenerDirector, base: str, email: str, password: str
) -> None:
    status, _, raw = request(
        opener, f"{base}/api/v1/auth/login", "POST", body={"email": email, "password": password}
    )
    token = _access_token(result, status, raw)
    if token is None:
        return
    auth = {"Authorization": f"Bearer {token}"}
    status, _, raw = request(opener, f"{base}/api/v1/accounts", headers=auth)
    accounts = _json(status, raw)
    ok = isinstance(accounts, list)
    result.check("list own accounts", ok, f"{status}, {len(accounts) if ok else 0} account(s)")
    status, _, _ = request(
        opener, f"{base}/api/v1/auth/logout", "POST", headers={**auth, **CSRF_HEADER}
    )
    result.check("logout", status == 204, str(status))


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url", help="https://<host> of the deployed stack")
    parser.add_argument("--cacert", help="PEM CA certificate to trust instead of the system store")
    parser.add_argument("--min-cert-days", type=int, default=14)
    parser.add_argument(
        "--check-http-redirect", action="store_true", help="port 80 is open (certbot)"
    )
    parser.add_argument("--http-port", type=int, default=80, help="for --check-http-redirect")
    parser.add_argument(
        "--no-login", action="store_true", help="skip the signed-in customer checks"
    )
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ

    parsed = urllib.parse.urlsplit(args.base_url.rstrip("/"))
    if parsed.scheme != "https" or not parsed.hostname:
        parser.error("base_url must be https://<host>[:port]")
    email, password = env.get("PBIP_SMOKE_EMAIL", ""), env.get("PBIP_SMOKE_PASSWORD", "")
    if not args.no_login and not (email and password):
        parser.error("set PBIP_SMOKE_EMAIL and PBIP_SMOKE_PASSWORD, or pass --no-login")

    base = f"https://{parsed.netloc}"
    context = tls_context(args.cacert)
    result = Result()
    sys.stdout.write(f"Staging smoke test against {base}\n")
    check_tls(result, parsed.hostname, parsed.port or 443, context, args.min_cert_days)
    if args.check_http_redirect:
        check_redirect(result, parsed.hostname, args.http_port)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),  # refresh cookie for logout
    )
    check_health(result, opener, base)
    if not args.no_login:
        check_customer(result, opener, base, email, password)
    if result.failures:
        sys.stdout.write(f"STAGING SMOKE TEST FAILED ({len(result.failures)})\n")
        return 1
    sys.stdout.write("STAGING SMOKE TEST PASSED\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
