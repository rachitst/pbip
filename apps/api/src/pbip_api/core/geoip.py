"""IP address to country code or approximate location, for fraud signals only (ADR-0014, 0034).

Country-level only, from the DB-IP "IP to Country Lite" database (CC BY 4.0, monthly, MMDB) read
with ``maxminddb``. The answer is approximate: anycast, VPN, mobile-carrier and cloud addresses are
often placed in the wrong country, and private, loopback or malformed addresses have no country.
Callers must treat ``None`` as "unknown", never as a location. Nothing is stored except the
two-letter code in the fraud feature snapshot; the IP itself is already in the security events.

``MmdbLocationResolver`` reads the DB-IP "IP to City Lite" database the same way and returns the
record's city-level point, for the IMPOSSIBLE_TRAVEL rule (ADR-0034). The point is where DB-IP
places the address block, not where the person is; it is kept only in the staff-only fraud
feature snapshot, never with the IP.
"""

from dataclasses import dataclass
from functools import lru_cache
from ipaddress import ip_address
from typing import Protocol, TypeGuard

import maxminddb
import structlog

_logger = structlog.get_logger(__name__)


class CountryResolver(Protocol):
    def country(self, ip: str | None) -> str | None:
        """ISO 3166-1 alpha-2 code for a public IP, or None when unknown."""


class NoCountryResolver:
    """Used when no database is configured: every lookup is unknown, so no geo rule can fire."""

    def country(self, ip: str | None) -> str | None:
        return None


class _Reader(Protocol):
    def get(self, ip_address: str) -> object: ...


class MmdbCountryResolver:
    """Looks up the country of a public IP in an MMDB country database."""

    def __init__(self, reader: _Reader) -> None:
        self._reader = reader

    @classmethod
    def open(cls, path: str) -> "MmdbCountryResolver":
        return cls(maxminddb.open_database(path))

    def country(self, ip: str | None) -> str | None:
        record = _lookup(self._reader, ip)
        if record is None:
            return None
        country = record.get("country")
        code = country.get("iso_code") if isinstance(country, dict) else None
        return code if isinstance(code, str) and len(code) == 2 else None


@lru_cache(maxsize=4)
def resolver_for(path: str | None) -> CountryResolver:
    """One reader per configured path (the database is memory-mapped and safe to share).

    A missing or unreadable file must not stop payments (ADR-0014): NEW_COUNTRY only ever adds
    risk, so it is switched off (every country is unknown) and the error is logged once per
    process. Restart after fixing the file (runbook 2.4).
    """
    if not path:
        return NoCountryResolver()
    try:
        return MmdbCountryResolver.open(path)
    except (OSError, maxminddb.InvalidDatabaseError) as exc:
        _logger.error("geoip_country_db_unavailable", reason=type(exc).__name__)
        return NoCountryResolver()


def _lookup(reader: _Reader, ip: str | None) -> dict[str, object] | None:
    """The record for a public IP; private, loopback, reserved and malformed are never looked up."""
    if not ip:
        return None
    try:
        address = ip_address(ip)
    except ValueError:
        return None
    if not address.is_global:
        return None
    record = reader.get(str(address))
    return record if isinstance(record, dict) else None


@dataclass(frozen=True, slots=True)
class GeoPoint:
    """Where the database places an address block (city level at best, ADR-0034)."""

    latitude: float
    longitude: float
    country: str | None = None
    city: str | None = None


class LocationResolver(Protocol):
    def locate(self, ip: str | None) -> GeoPoint | None:
        """Approximate point for a public IP, or None when unknown."""


class NoLocationResolver:
    """Used when no city database is configured: nothing is located, so no travel rule can fire."""

    def locate(self, ip: str | None) -> GeoPoint | None:
        return None


_CITY_NAME_MAX = 60


class MmdbLocationResolver:
    """Looks up the city-level point of a public IP in an MMDB city database (DB-IP City Lite)."""

    def __init__(self, reader: _Reader) -> None:
        self._reader = reader

    @classmethod
    def open(cls, path: str) -> "MmdbLocationResolver":
        return cls(maxminddb.open_database(path))

    def locate(self, ip: str | None) -> GeoPoint | None:
        record = _lookup(self._reader, ip)
        if record is None:
            return None
        location = record.get("location")
        if not isinstance(location, dict):
            return None
        lat, lon = location.get("latitude"), location.get("longitude")
        if not _coordinate(lat, 90) or not _coordinate(lon, 180):
            return None
        country = record.get("country")
        code = country.get("iso_code") if isinstance(country, dict) else None
        return GeoPoint(
            latitude=float(lat),
            longitude=float(lon),
            country=code if isinstance(code, str) and len(code) == 2 else None,
            city=_city_name(record.get("city")),
        )


def _coordinate(value: object, bound: int) -> TypeGuard[float]:
    return (
        isinstance(value, int | float) and not isinstance(value, bool) and -bound <= value <= bound
    )


def _city_name(city: object) -> str | None:
    """English city name, printable characters only and capped: it ends up in analyst reasons."""
    names = city.get("names") if isinstance(city, dict) else None
    name = names.get("en") if isinstance(names, dict) else None
    if not isinstance(name, str):
        return None
    cleaned = "".join(ch for ch in name if ch.isprintable()).strip()[:_CITY_NAME_MAX]
    return cleaned or None


@lru_cache(maxsize=4)
def location_resolver_for(path: str | None) -> LocationResolver:
    """One reader per configured path (memory-mapped, shared, read-only).

    A missing or unreadable file must not stop payments: the travel rule only ever adds risk, so
    it is switched off (every payment records ``location_unknown``) and the error is logged once
    per process. Restart after fixing the file (runbook 2.4).
    """
    if not path:
        return NoLocationResolver()
    try:
        return MmdbLocationResolver.open(path)
    except (OSError, maxminddb.InvalidDatabaseError) as exc:
        _logger.error("geoip_city_db_unavailable", reason=type(exc).__name__)
        return NoLocationResolver()
