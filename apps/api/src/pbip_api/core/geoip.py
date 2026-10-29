"""IP address to country code, for fraud signals only (ADR-0014).

Country-level only, from the DB-IP "IP to Country Lite" database (CC BY 4.0, monthly, MMDB) read
with ``maxminddb``. The answer is approximate: anycast, VPN, mobile-carrier and cloud addresses are
often placed in the wrong country, and private, loopback or malformed addresses have no country.
Callers must treat ``None`` as "unknown", never as a location. Nothing is stored except the
two-letter code in the fraud feature snapshot; the IP itself is already in the security events.
"""

from functools import lru_cache
from ipaddress import ip_address
from typing import Protocol

import maxminddb


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
        if not ip:
            return None
        try:
            address = ip_address(ip)
        except ValueError:
            return None
        if not address.is_global:
            return None
        record = self._reader.get(str(address))
        if not isinstance(record, dict):
            return None
        country = record.get("country")
        code = country.get("iso_code") if isinstance(country, dict) else None
        return code if isinstance(code, str) and len(code) == 2 else None


@lru_cache(maxsize=4)
def resolver_for(path: str | None) -> CountryResolver:
    """One reader per configured path (the database is memory-mapped and safe to share)."""
    if not path:
        return NoCountryResolver()
    return MmdbCountryResolver.open(path)
