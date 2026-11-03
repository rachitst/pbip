"""Download and verify a DB-IP Lite database for the geographic fraud rules.

``--product country`` (default) is "IP to Country Lite" for NEW_COUNTRY (ADR-0014);
``--product city`` is "IP to City Lite" for IMPOSSIBLE_TRAVEL (ADR-0034, about 60 MB download,
120 MB file). DB-IP publishes both monthly under CC BY 4.0 (attribution: "IP Geolocation by DB-IP",
https://db-ip.com). It is not committed to git: run this on the host, then point
GEOIP_COUNTRY_DB_PATH / GEOIP_CITY_DB_PATH at the result (runbook section 2.4). The file is
verified by looking up two well-known addresses (and, for the city file, that they have a point)
before it replaces the previous copy, so a bad download never reaches the API.

Usage:
    uv run python scripts/fetch_geoip.py [--product city] [--month 2026-10] [--dest PATH]
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

URL = "https://download.db-ip.com/free/dbip-{product}-lite-{month}.mmdb.gz"
PRODUCTS = ("country", "city")
CHECKS = {"8.8.8.8": "US", "81.2.69.142": "GB"}


def url_for(month: str, product: str = "country") -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        msg = f"month must be YYYY-MM, got {month!r}"
        raise ValueError(msg)
    if product not in PRODUCTS:
        msg = f"product must be one of {PRODUCTS}, got {product!r}"
        raise ValueError(msg)
    return URL.format(product=product, month=month)


def verify(path: Path) -> None:
    """The file is a country MMDB that places well-known addresses as expected."""
    from pbip_api.core.geoip import MmdbCountryResolver

    resolver = MmdbCountryResolver.open(str(path))
    for ip, expected in CHECKS.items():
        got = resolver.country(ip)
        if got != expected:
            msg = f"verification failed: {ip} -> {got!r}, expected {expected!r}"
            raise ValueError(msg)


def verify_city(path: Path) -> None:
    """A city MMDB: the country checks hold and each address also has a point."""
    from pbip_api.core.geoip import MmdbLocationResolver

    verify(path)
    resolver = MmdbLocationResolver.open(str(path))
    for ip, expected in CHECKS.items():
        point = resolver.locate(ip)
        if point is None or point.country != expected:
            msg = f"verification failed: {ip} has no point in {expected!r}"
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
    product: str = "country",
) -> str:
    """Download, decompress, verify, then atomically replace ``dest``. Returns the SHA-256."""
    data = gzip.decompress(get(url_for(month, product)))
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
    parser.add_argument("--product", choices=PRODUCTS, default="country")
    parser.add_argument("--month", default=datetime.now(UTC).strftime("%Y-%m"))
    parser.add_argument("--dest", type=Path)
    args = parser.parse_args()
    dest = args.dest or Path(f"data/geoip/dbip-{args.product}-lite.mmdb")
    check = verify_city if args.product == "city" else verify
    digest = fetch(args.month, dest, check=check, product=args.product)
    sys.stdout.write(f"Wrote {dest} ({args.product}, {args.month}), sha256 {digest}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
