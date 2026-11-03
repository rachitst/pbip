"""scripts/fetch_geoip.py: URL, atomic replace, and no replacement when verification fails."""

import gzip
from pathlib import Path

import pytest

from scripts.fetch_geoip import fetch, url_for, verify_city

pytestmark = pytest.mark.unit


def test_url_is_built_from_a_valid_month_only() -> None:
    assert url_for("2026-10") == "https://download.db-ip.com/free/dbip-country-lite-2026-10.mmdb.gz"
    with pytest.raises(ValueError, match="YYYY-MM"):
        url_for("2026-13")
    with pytest.raises(ValueError, match="YYYY-MM"):
        url_for("../etc")


def test_city_product_url_and_unknown_products_are_rejected() -> None:
    assert (
        url_for("2026-11", "city")
        == "https://download.db-ip.com/free/dbip-city-lite-2026-11.mmdb.gz"
    )
    with pytest.raises(ValueError, match="product"):
        url_for("2026-11", "asn")


def test_city_download_is_fetched_from_the_city_url(tmp_path: Path) -> None:
    asked: list[str] = []

    def get(url: str) -> bytes:
        asked.append(url)
        return gzip.compress(b"city")

    fetch("2026-11", tmp_path / "c.mmdb", get=get, check=lambda _p: None, product="city")
    assert asked == ["https://download.db-ip.com/free/dbip-city-lite-2026-11.mmdb.gz"]


def test_a_country_only_file_fails_city_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The city check needs a point, so a country file saved under the city name is refused."""
    from pbip_api.core import geoip

    class _CountryOnly:
        def get(self, ip_address: str) -> object:
            return {"country": {"iso_code": {"8.8.8.8": "US"}.get(ip_address, "GB")}}

    monkeypatch.setattr(geoip.maxminddb, "open_database", lambda _path: _CountryOnly())
    with pytest.raises(ValueError, match="no point"):
        verify_city(tmp_path / "x.mmdb")


def test_verified_download_replaces_the_previous_file(tmp_path: Path) -> None:
    dest = tmp_path / "geo" / "db.mmdb"
    dest.parent.mkdir()
    dest.write_bytes(b"old")
    digest = fetch("2026-10", dest, get=lambda _url: gzip.compress(b"new"), check=lambda _p: None)
    assert dest.read_bytes() == b"new" and len(digest) == 64
    assert not (tmp_path / "geo" / "db.mmdb.new").exists()


def test_a_download_that_fails_verification_never_replaces_the_previous_file(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "db.mmdb"
    dest.write_bytes(b"old")

    def reject(_path: Path) -> None:
        raise ValueError("verification failed")

    with pytest.raises(ValueError, match="verification failed"):
        fetch("2026-10", dest, get=lambda _url: gzip.compress(b"bad"), check=reject)
    assert dest.read_bytes() == b"old"
    assert not (tmp_path / "db.mmdb.new").exists()
