"""Download and verify the DB-IP "IP to Country Lite" database for the NEW_COUNTRY fraud rule.

DB-IP publishes the file monthly under CC BY 4.0 (attribution: "IP Geolocation by DB-IP",
https://db-ip.com). It is not committed to git: run this on the host, then point
GEOIP_COUNTRY_DB_PATH at the result (ADR-0014, runbook section 2.4). The file is verified by
opening it as a country MMDB and looking up two well-known addresses before it replaces the
previous copy, so a bad download never reaches the API.

Usage:
    uv run python scripts/fetch_geoip.py [--month 2026-10] [--dest data/geoip/country.mmdb]
"""

import argparse
import gzip
import hashlib
import re
import sys
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

URL = "https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz"
DEFAULT_DEST = Path("data/geoip/dbip-country-lite.mmdb")
CHECKS = {"8.8.8.8": "US", "81.2.69.142": "GB"}


def url_for(month: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        msg = f"month must be YYYY-MM, got {month!r}"
        raise ValueError(msg)
    return URL.format(month=month)


def verify(path: Path) -> None:
    """The file is a country MMDB that places well-known addresses as expected."""
    from pbip_api.core.geoip import MmdbCountryResolver

    resolver = MmdbCountryResolver.open(str(path))
    for ip, expected in CHECKS.items():
        got = resolver.country(ip)
        if got != expected:
            msg = f"verification failed: {ip} -> {got!r}, expected {expected!r}"
            raise ValueError(msg)


def download(url: str) -> bytes:
    # DB-IP answers 403 to Python's default User-Agent; identify the client explicitly.
    request = urllib.request.Request(url, headers={"User-Agent": "pbip-fetch-geoip/1.0"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - fixed https URL
        return bytes(response.read())


def fetch(
    month: str,
    dest: Path,
    get: Callable[[str], bytes] = download,
    check: Callable[[Path], None] = verify,
) -> str:
    """Download, decompress, verify, then atomically replace ``dest``. Returns the SHA-256."""
    data = gzip.decompress(get(url_for(month)))
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = dest.with_suffix(dest.suffix + ".new")
    staged.write_bytes(data)
    try:
        check(staged)
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    staged.replace(dest)
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--month", default=datetime.now(UTC).strftime("%Y-%m"))
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = parser.parse_args()
    digest = fetch(args.month, args.dest)
    sys.stdout.write(f"Wrote {args.dest} ({args.month}), sha256 {digest}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
