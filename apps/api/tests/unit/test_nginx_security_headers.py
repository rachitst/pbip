"""The static-site security headers nginx sends (infra/nginx/security-headers.conf).

TASK-090: the OWASP ZAP baseline flagged missing Spectre site isolation (rule 90004) on static responses;
this keeps the full header set from regressing. verify_tls.sh checks the live stack in CI's e2e job.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
CONF = ROOT / "infra" / "nginx" / "security-headers.conf"
TEMPLATE = ROOT / "infra" / "nginx" / "default.conf.template"


def _headers() -> dict[str, str]:
    pattern = re.compile(r'^add_header\s+([\w-]+)\s+"([^"]*)"\s+always;$')
    found = {}
    for line in CONF.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line.strip())
        if match:
            found[match.group(1)] = match.group(2)
    return found


def test_static_responses_carry_the_full_security_header_set() -> None:
    headers = _headers()
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert headers["Cross-Origin-Embedder-Policy"] == "require-corp"
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert headers["Strict-Transport-Security"].startswith("max-age=63072000")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    csp = headers["Content-Security-Policy"]
    for directive in ("default-src 'self'", "script-src 'self'", "frame-ancestors 'none'"):
        assert directive in csp


def test_every_static_location_includes_the_header_file() -> None:
    """add_header is not inherited once a location sets its own, so each static location must include it."""
    template = TEMPLATE.read_text(encoding="utf-8")
    for location in ("location /assets/", "location / {\n        include"):
        assert location in template or location.split(" {")[0] in template
    assert template.count("include /etc/nginx/pbip-security-headers.conf;") == 2
