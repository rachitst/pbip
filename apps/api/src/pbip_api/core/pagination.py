"""Opaque keyset cursors for ledger-like, append-only data.

A cursor names the last row a client has seen: its immutable sequence number (the keyset) and its
timestamp (so a client or an operator can tell *when* a page position is). Ordering and filtering
use ``seq`` alone. For ledger entries that is the posting order: entries for one account are
written under that account's row lock, so ``seq`` order equals commit order. ``created_at`` is the
DB transaction start time and is not monotonic under contention, so it must not drive the keyset.

Cursors are not secrets and grant nothing: the caller's object authorization is checked before a
cursor is applied, and the cursor only narrows a query that is already scoped to an owned object.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pbip_api.core.errors import UnprocessableError

_VERSION = 1
MAX_CURSOR_LENGTH = 200
MAX_SEQ = 2**63 - 1  # transaction_entries.seq is BIGINT; larger values overflow in SQL (500)


class PageDirection(StrEnum):
    OLDER = "older"  # walk back in time; items newest-first
    NEWER = "newer"  # walk forward in time from the cursor; items oldest-first


@dataclass(frozen=True, slots=True)
class Cursor:
    seq: int
    ts: datetime | None = None  # None only for legacy integer cursors


def encode_cursor(seq: int, ts: datetime) -> str:
    payload = json.dumps({"v": _VERSION, "seq": seq, "ts": ts.isoformat()}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _invalid() -> UnprocessableError:
    return UnprocessableError("invalid pagination cursor")


def decode_cursor(raw: str) -> Cursor:
    """Parse a cursor. Bare positive integers (the pre-v1.1 format) are still accepted."""
    if not raw or len(raw) > MAX_CURSOR_LENGTH:
        raise _invalid()
    if raw.isascii() and raw.isdigit():
        seq = int(raw)
        if not 1 <= seq <= MAX_SEQ:
            raise _invalid()
        return Cursor(seq=seq)
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        seq, ts = payload["seq"], datetime.fromisoformat(payload["ts"])
        version_ok = payload["v"] == _VERSION
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise _invalid() from exc
    if not version_ok or type(seq) is not int or not 1 <= seq <= MAX_SEQ or ts.tzinfo is None:
        raise _invalid()
    return Cursor(seq=seq, ts=ts)
