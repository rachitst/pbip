"""scripts/fetch_geoip.py: URL, atomic replace, and no replacement when verification fails."""

import gzip
from pathlib import Path

import pytest

from scripts.fetch_geoip import fetch, url_for

pytestmark = pytest.mark.unit


def test_url_is_built_from_a_valid_month_only() -> None:
    assert url_for("2026-10") == "https://download.db-ip.com/free/dbip-country-lite-2026-10.mmdb.gz"
    with pytest.raises(ValueError, match="YYYY-MM"):
        url_for("2026-13")
    with pytest.raises(ValueError, match="YYYY-MM"):
        url_for("../etc")


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
