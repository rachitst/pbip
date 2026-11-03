"""scripts/staging_smoke.py against a local HTTPS stand-in for the deployed stack (#198).

A throwaway CA and a `localhost` leaf (generated per run) serve a tiny fake API over real TLS, so the
script's certificate verification, HSTS check, login -> read-only call -> logout sequence and its
promise to print no credential, token or account number are all exercised without a host.
"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import secrets
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import scripts.staging_smoke as smoke

pytestmark = pytest.mark.unit

EMAIL = "smoke@example.com"
PASSWORD = secrets.token_urlsafe(16)  # generated per run: no credential literal in tests
TOKEN = secrets.token_urlsafe(24)
ACCOUNT_NUMBER = "50100012345678"
HSTS = "max-age=63072000; includeSubDomains"


@dataclass
class FakeApi:
    hsts: bool = True
    mfa: bool = False
    logout_cookie: str | None = None
    requests: list[str] = field(default_factory=list)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _write_pki(directory: Path, leaf_days: int) -> tuple[Path, Path, Path]:
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("smoke test CA"))
        .issuer_name(_name("smoke test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=leaf_days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    paths = directory / "ca.crt", directory / "tls.crt", directory / "tls.key"
    paths[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


class Handler(http.server.BaseHTTPRequestHandler):
    """Answers the five calls the script makes; ``api`` is set per server by ``_handler``."""

    api: FakeApi

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        for name, value in {**(headers or {}), "Content-Length": str(len(raw))}.items():
            self.send_header(name, value)
        if self.api.hsts:
            self.send_header("Strict-Transport-Security", HSTS)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self.api.requests.append(f"GET {self.path}")
        if self.path in ("/api/health/live", "/api/health/ready"):
            self._send(200, {"status": "ok"})
        elif self.path != "/api/v1/accounts":
            self._send(404)
        elif self.headers.get("Authorization") == f"Bearer {TOKEN}":
            self._send(200, [{"account_number": ACCOUNT_NUMBER, "available_minor": 0}])
        else:
            self._send(401, {"title": "Unauthorized"})

    def do_POST(self) -> None:
        self.api.requests.append(f"POST {self.path}")
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/api/v1/auth/login":
            self._login(json.loads(body))
        elif self.path == "/api/v1/auth/logout":
            self._logout()
        else:
            self._send(404)

    def _login(self, body: Any) -> None:
        if body != {"email": EMAIL, "password": PASSWORD}:
            self._send(401, {"title": "Unauthorized"})
        elif self.api.mfa:
            self._send(200, {"mfa_required": True, "mfa_token": "x"})
        else:
            cookie = "pbip_refresh=r1; Path=/api/v1/auth; Secure; HttpOnly; SameSite=Strict"
            self._send(200, {"access_token": TOKEN}, {"Set-Cookie": cookie})

    def _logout(self) -> None:
        if self.headers.get("X-Requested-With") != "pbip-web":  # the API's csrf_guard
            self._send(403)
            return
        self.api.logout_cookie = self.headers.get("Cookie")
        self._send(204)


def _handler(api: FakeApi) -> type[Handler]:
    return type("BoundHandler", (Handler,), {"api": api})


@dataclass
class Stack:
    url: str
    ca: Path
    api: FakeApi


def _serve(tmp_path: Path, leaf_days: int = 60) -> Iterator[Stack]:
    ca, cert, key = _write_pki(tmp_path, leaf_days)
    api = FakeApi()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(api))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Stack(f"https://localhost:{server.server_address[1]}", ca, api)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    yield from _serve(tmp_path)


ENV = {"PBIP_SMOKE_EMAIL": EMAIL, "PBIP_SMOKE_PASSWORD": PASSWORD}


def test_passes_against_a_healthy_stack_and_signs_out(
    stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    assert smoke.main([stack.url, "--cacert", str(stack.ca)], environ=ENV) == 0
    out = capsys.readouterr().out
    assert "STAGING SMOKE TEST PASSED" in out
    assert "chain and host name verified" in out and "1 account(s)" in out
    assert stack.api.requests == [
        "GET /api/health/live",
        "GET /api/health/ready",
        "POST /api/v1/auth/login",
        "GET /api/v1/accounts",
        "POST /api/v1/auth/logout",
    ]
    assert stack.api.logout_cookie == "pbip_refresh=r1"  # the session it created is revoked


def test_prints_no_credential_token_or_account_number(
    stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    smoke.main([stack.url, "--cacert", str(stack.ca)], environ=ENV)
    out = capsys.readouterr()
    for secret in (EMAIL, PASSWORD, TOKEN, ACCOUNT_NUMBER):
        assert secret not in out.out and secret not in out.err


def test_an_untrusted_certificate_fails_the_tls_check(
    stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    assert smoke.main([stack.url, "--no-login"], environ={}) == 1  # system store: CA unknown
    assert "FAIL TLS handshake" in capsys.readouterr().out


def test_a_certificate_close_to_expiry_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for stack in _serve(tmp_path, leaf_days=5):
        assert smoke.main([stack.url, "--cacert", str(stack.ca), "--no-login"], environ={}) == 1
    assert "FAIL certificate validity" in capsys.readouterr().out


def test_missing_hsts_fails(stack: Stack, capsys: pytest.CaptureFixture[str]) -> None:
    stack.api.hsts = False
    assert smoke.main([stack.url, "--cacert", str(stack.ca)], environ=ENV) == 1
    assert "FAIL HSTS on API responses" in capsys.readouterr().out


def test_wrong_password_fails_without_further_calls(stack: Stack) -> None:
    env = {**ENV, "PBIP_SMOKE_PASSWORD": secrets.token_urlsafe(16)}
    assert smoke.main([stack.url, "--cacert", str(stack.ca)], environ=env) == 1
    assert "GET /api/v1/accounts" not in stack.api.requests


def test_a_user_with_an_authenticator_is_refused_with_a_clear_message(
    stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    stack.api.mfa = True
    assert smoke.main([stack.url, "--cacert", str(stack.ca)], environ=ENV) == 1
    assert "has an authenticator" in capsys.readouterr().out


def test_credentials_are_required_unless_login_is_skipped(stack: Stack) -> None:
    with pytest.raises(SystemExit) as exc:
        smoke.main([stack.url, "--cacert", str(stack.ca)], environ={})
    assert exc.value.code == 2
    assert smoke.main([stack.url, "--cacert", str(stack.ca), "--no-login"], environ={}) == 0


def test_plain_http_base_urls_are_rejected() -> None:
    with pytest.raises(SystemExit):
        smoke.main(["http://staging.example", "--no-login"], environ={})
