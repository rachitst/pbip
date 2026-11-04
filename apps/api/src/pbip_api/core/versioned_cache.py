"""Bounded in-process cache whose entries are valid only for the version they were stored under
(ADR-0053).

The caller reads the version from the database on every request (for example the row versions of
the customer's accounts), so a hit is never older than the data it was derived from: any write
that changes the inputs changes the version, and the stale entry is simply not used. There is no
invalidation call to forget and nothing to propagate between API replicas. The TTL only bounds how
long an entry is kept; it is not what keeps it correct.

Keys are the authenticated customer's id (plus whatever else the caller needs), never a value a
client chose, so one customer's entry cannot be served to another.
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass

from pbip_api.core.metrics import READ_CACHE_LOOKUPS_TOTAL


@dataclass(frozen=True, slots=True)
class _Entry[V]:
    version: Hashable
    value: V
    stored_at: float


class VersionedCache[K: Hashable, V]:
    """Least-recently-used, at most ``max_entries`` entries, thread-safe (sync routes run on a
    thread pool). Store only immutable values: a hit returns the stored object itself."""

    def __init__(
        self,
        name: str,
        max_entries: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1 or ttl_seconds <= 0:
            msg = "max_entries must be >= 1 and ttl_seconds > 0"
            raise ValueError(msg)
        self.name = name
        self._max = max_entries
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[K, _Entry[V]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: K, version: Hashable) -> V | None:
        """The value stored for ``key`` under exactly ``version``, or None (a miss)."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (
                entry.version != version or self._clock() - entry.stored_at >= self._ttl
            ):
                del self._entries[key]
                entry = None
            if entry is not None:
                self._entries.move_to_end(key)
        READ_CACHE_LOOKUPS_TOTAL.labels(cache=self.name, outcome="hit" if entry else "miss").inc()
        return entry.value if entry is not None else None

    def put(self, key: K, version: Hashable, value: V) -> None:
        with self._lock:
            self._entries[key] = _Entry(version, value, self._clock())
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
