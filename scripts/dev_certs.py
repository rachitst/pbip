"""Generate LOCAL development TLS material for the compose stack (never used outside this machine).

    uv run python scripts/dev_certs.py [--force] [--out infra/certs]

Writes a throwaway local CA and two leaf certificates signed by it (all git-ignored):

    infra/certs/ca.crt              trust anchor: curl --cacert, SSL_CERT_FILE, sslrootcert
    infra/certs/ca.key              CA private key (0600; only this script reads it)
    infra/certs/web/tls.{crt,key}   nginx (web) - SAN localhost, 127.0.0.1, ::1, web
    infra/certs/db/server.{crt,key} PostgreSQL (db) - SAN db, localhost, 127.0.0.1

A CA (rather than one self-signed leaf) lets clients VERIFY the chain: the API connects to
PostgreSQL with ``sslmode=verify-full`` and CI checks nginx with ``curl --cacert``. Existing files
are kept unless --force is given or a leaf expires within 30 days, so re-running is safe.

Staging/production use certbot for nginx and ``infra/deploy/deploy.sh`` for the database
(docs/operations/runbook.md section 1.4); this script is for localhost only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "infra" / "certs"
CA_DAYS = 825
LEAF_DAYS = 397  # the browser maximum for a leaf certificate
RENEW_WITHIN = dt.timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Leaf:
    directory: str
    cert_name: str
    key_name: str
    common_name: str
    dns: tuple[str, ...]
    ips: tuple[str, ...]
    # nginx-unprivileged (uid 101) must read the web key through a bind mount, so it is 0644;
    # the db key is copied and re-owned by the postgres entrypoint, so it stays 0600.
    key_mode: int


LEAVES = (
    Leaf(
        "web", "tls.crt", "tls.key", "localhost", ("localhost", "web"), ("127.0.0.1", "::1"), 0o644
    ),
    Leaf("db", "server.crt", "server.key", "db", ("db", "localhost"), ("127.0.0.1",), 0o600),
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)  # no-op for the group/other bits on Windows; the stack runs on Linux


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),  # dev-only, git-ignored; services must start unattended
    )


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PBIP local development"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _build_ca(out: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = _name("PBIP local development CA")
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write(out / "ca.key", _key_pem(key), 0o600)
    _write(out / "ca.crt", cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return cert, key


def _build_leaf(
    out: Path, leaf: Leaf, ca: x509.Certificate, ca_key: ec.EllipticCurvePrivateKey
) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = [x509.DNSName(d) for d in leaf.dns]
    sans += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in leaf.ips]
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(leaf.common_name))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    directory = out / leaf.directory
    _write(directory / leaf.key_name, _key_pem(key), leaf.key_mode)
    _write(directory / leaf.cert_name, cert.public_bytes(serialization.Encoding.PEM), 0o644)


def _is_current(cert_path: Path) -> bool:
    if not cert_path.exists():
        return False
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert.not_valid_after_utc - _now() > RENEW_WITHIN


def _load_ca(out: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey] | None:
    cert_path, key_path = out / "ca.crt", out / "ca.key"
    if not (_is_current(cert_path) and key_path.exists()):
        return None
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        return None
    return x509.load_pem_x509_certificate(cert_path.read_bytes()), key


def generate(out: Path = DEFAULT_OUT, force: bool = False) -> list[str]:
    """Create whatever is missing or expiring; returns the relative paths written."""
    written: list[str] = []
    loaded = None if force else _load_ca(out)
    if loaded is None:
        loaded = _build_ca(out)
        written.append("ca.crt")
        force = True  # a new CA invalidates every leaf signed by the old one
    ca, ca_key = loaded
    for leaf in LEAVES:
        if force or not _is_current(out / leaf.directory / leaf.cert_name):
            _build_leaf(out, leaf, ca, ca_key)
            written.append(f"{leaf.directory}/{leaf.cert_name}")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--force", action="store_true", help="regenerate the CA and every leaf")
    args = parser.parse_args(argv)
    written = generate(args.out, args.force)
    if written:
        sys.stdout.write(f"Wrote {', '.join(written)} under {args.out} (dev-only, git-ignored)\n")
    else:
        sys.stdout.write(f"Dev TLS material in {args.out} is current; nothing changed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
