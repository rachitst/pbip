"""Device and network identity for fraud signals (TASK-095, ADR-0020).

The web client sends a random per-browser id as ``X-Device-ID`` (``lib/device.ts``). The platform
never stores it: the device registry keeps its SHA-256, and fraud scoring uses a UUIDv5 of that
hash, so the same browser always maps to the same device id and the raw value goes nowhere.

The network is the client IP truncated to its /24 (IPv4) or /48 (IPv6) prefix: enough to tell
"the usual home or office network" from "somewhere new", without keeping the full address in
the fraud snapshot.
"""

import hashlib
import ipaddress
from uuid import UUID, uuid5

# Fixed namespace so device ids are stable across processes and releases.
_DEVICE_NAMESPACE = UUID("5f0c3a64-0f6e-4d7e-9a43-6b1e2f0d9c11")


def fingerprint_hash(raw: str) -> str:
    """Same hash the device registry stores (``customers.service.register_device``)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def device_uuid_from_hash(digest: str) -> UUID:
    return uuid5(_DEVICE_NAMESPACE, digest)


def device_uuid(raw: str | None) -> UUID | None:
    if raw is None or not raw.strip():
        return None
    return device_uuid_from_hash(fingerprint_hash(raw.strip()))


def ip_network(ip: str | None) -> str | None:
    """``203.0.113.7`` -> ``203.0.113.0/24``; ``2001:db8:1:2::5`` -> ``2001:db8:1::/48``."""
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    prefix = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
