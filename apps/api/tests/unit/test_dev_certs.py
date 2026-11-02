"""scripts/dev_certs.py: the local CA and leaf certificates the HTTPS compose stack depends on (NFR-02)."""

from __future__ import annotations

import datetime as dt
import ipaddress
import stat
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

import scripts.dev_certs as certs

pytestmark = pytest.mark.unit


def _cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _sans(cert: x509.Certificate) -> tuple[set[str], set[str]]:
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return set(ext.get_values_for_type(x509.DNSName)), {
        str(ip) for ip in ext.get_values_for_type(x509.IPAddress)
    }


def test_generates_a_ca_and_leaves_that_chain_to_it(tmp_path: Path) -> None:
    written = certs.generate(tmp_path)
    assert written == ["ca.crt", "web/tls.crt", "db/server.crt"]
    ca = _cert(tmp_path / "ca.crt")
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    ca_key = ca.public_key()
    assert isinstance(ca_key, ec.EllipticCurvePublicKey)
    for leaf_path in (tmp_path / "web" / "tls.crt", tmp_path / "db" / "server.crt"):
        leaf = _cert(leaf_path)
        leaf.verify_directly_issued_by(ca)  # raises if the CA did not sign it
        assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
        eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert ExtendedKeyUsageOID.SERVER_AUTH in eku
        lifetime = leaf.not_valid_after_utc - leaf.not_valid_before_utc
        assert lifetime <= dt.timedelta(days=398)  # browsers reject longer-lived leaves


def test_leaf_names_match_how_clients_connect(tmp_path: Path) -> None:
    certs.generate(tmp_path)
    web_dns, web_ips = _sans(_cert(tmp_path / "web" / "tls.crt"))
    assert {"localhost", "web"} <= web_dns and {"127.0.0.1", "::1"} <= web_ips
    db_dns, db_ips = _sans(_cert(tmp_path / "db" / "server.crt"))
    # The API uses sslmode=verify-full against host "db"; host tools use 127.0.0.1.
    assert "db" in db_dns and "127.0.0.1" in db_ips
    assert all(ipaddress.ip_address(ip) for ip in web_ips | db_ips)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_private_key_permissions(tmp_path: Path) -> None:
    certs.generate(tmp_path)

    def mode(p: Path) -> int:
        return stat.S_IMODE(p.stat().st_mode)

    assert mode(tmp_path / "ca.key") == 0o600
    assert mode(tmp_path / "db" / "server.key") == 0o600  # re-owned inside the db container
    assert (
        mode(tmp_path / "web" / "tls.key") == 0o644
    )  # nginx-unprivileged reads it via a bind mount


def test_rerun_keeps_current_material_and_force_replaces_it(tmp_path: Path) -> None:
    certs.generate(tmp_path)
    before = (tmp_path / "ca.crt").read_bytes(), (tmp_path / "web" / "tls.crt").read_bytes()
    assert certs.generate(tmp_path) == []
    assert (
        (tmp_path / "ca.crt").read_bytes(),
        (tmp_path / "web" / "tls.crt").read_bytes(),
    ) == before
    assert certs.generate(tmp_path, force=True) == ["ca.crt", "web/tls.crt", "db/server.crt"]
    assert (tmp_path / "ca.crt").read_bytes() != before[0]


def test_missing_or_expiring_leaf_is_reissued_by_the_existing_ca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    certs.generate(tmp_path)
    ca_before = (tmp_path / "ca.crt").read_bytes()
    (tmp_path / "db" / "server.crt").unlink()
    assert certs.generate(tmp_path) == ["db/server.crt"]
    _cert(tmp_path / "db" / "server.crt").verify_directly_issued_by(_cert(tmp_path / "ca.crt"))
    # 380 days later every leaf is inside the 30-day renewal window, the CA (825 days) is not.
    later = certs._now() + dt.timedelta(days=380)
    monkeypatch.setattr(certs, "_now", lambda: later)
    assert certs.generate(tmp_path) == ["web/tls.crt", "db/server.crt"]
    assert (tmp_path / "ca.crt").read_bytes() == ca_before


def test_a_replaced_ca_reissues_every_leaf(tmp_path: Path) -> None:
    certs.generate(tmp_path)
    (tmp_path / "ca.key").unlink()  # CA unusable: the old leaves can no longer be renewed
    assert certs.generate(tmp_path) == ["ca.crt", "web/tls.crt", "db/server.crt"]
    ca = _cert(tmp_path / "ca.crt")
    _cert(tmp_path / "web" / "tls.crt").verify_directly_issued_by(ca)


def test_cli_reports_what_it_did(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert certs.main(["--out", str(tmp_path)]) == 0
    assert "Wrote ca.crt" in capsys.readouterr().out
    assert certs.main(["--out", str(tmp_path)]) == 0
    assert "nothing changed" in capsys.readouterr().out
