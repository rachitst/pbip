"""IP-to-country lookups for the NEW_COUNTRY fraud rule (ADR-0014)."""

import os
from pathlib import Path

import pytest
from pbip_api.core.geoip import MmdbCountryResolver, NoCountryResolver, resolver_for

pytestmark = pytest.mark.unit


class _FakeReader:
    def __init__(self, records: dict[str, object]) -> None:
        self.records = records
        self.asked: list[str] = []

    def get(self, ip_address: str) -> object:
        self.asked.append(ip_address)
        return self.records.get(ip_address)


def test_no_database_means_every_country_is_unknown() -> None:
    assert NoCountryResolver().country("49.36.0.1") is None
    assert isinstance(resolver_for(None), NoCountryResolver)
    assert isinstance(resolver_for(""), NoCountryResolver)


def test_public_ip_returns_the_records_iso_code() -> None:
    reader = _FakeReader({"49.36.0.1": {"country": {"iso_code": "IN"}}})
    assert MmdbCountryResolver(reader).country("49.36.0.1") == "IN"


@pytest.mark.parametrize(
    "ip",
    [
        "10.1.2.3",
        "192.168.0.10",
        "127.0.0.1",
        "::1",
        "fd00::1",
        "169.254.1.1",
        "not-an-ip",
        "",
        None,
    ],
)
def test_private_loopback_and_malformed_addresses_are_never_looked_up(ip: str | None) -> None:
    reader = _FakeReader({})
    assert MmdbCountryResolver(reader).country(ip) is None
    assert reader.asked == []


@pytest.mark.parametrize(
    "record", [None, "XX", {}, {"country": None}, {"country": {"iso_code": "GBR"}}, {"country": {}}]
)
def test_missing_or_malformed_records_are_unknown(record: object) -> None:
    reader = _FakeReader({"81.2.69.142": record})
    assert MmdbCountryResolver(reader).country("81.2.69.142") is None


@pytest.mark.skipif(
    not os.environ.get("PBIP_GEOIP_TEST_DB"),
    reason="set PBIP_GEOIP_TEST_DB to a DB-IP country MMDB",
)
def test_real_dbip_database_places_well_known_addresses() -> None:
    """Manual check against the downloaded DB-IP file (no network in tests; skipped in CI)."""
    path = os.environ["PBIP_GEOIP_TEST_DB"]
    assert Path(path).is_file()
    resolver = MmdbCountryResolver.open(path)
    assert resolver.country("8.8.8.8") == "US"
    assert resolver.country("81.2.69.142") == "GB"
    assert resolver.country("10.0.0.1") is None
