"""VersionedCache (ADR-0053): an entry is used only under the exact version it was stored with."""

import pytest
from pbip_api.core.metrics import READ_CACHE_LOOKUPS_TOTAL
from pbip_api.core.versioned_cache import VersionedCache

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _cache(clock: Clock, max_entries: int = 3) -> VersionedCache[str, str]:
    return VersionedCache("test", max_entries=max_entries, ttl_seconds=60, clock=clock)


def test_a_hit_needs_the_same_key_and_the_same_version() -> None:
    cache = _cache(Clock())
    cache.put("alice", (1, "2026-11-04"), "figures")
    assert cache.get("alice", (1, "2026-11-04")) == "figures"
    assert cache.get("bob", (1, "2026-11-04")) is None
    assert cache.get("alice", (2, "2026-11-04")) is None
    # a mismatch drops the entry: going back to the old version does not revive it
    assert cache.get("alice", (1, "2026-11-04")) is None
    assert len(cache) == 0


def test_an_entry_expires_after_the_ttl() -> None:
    clock = Clock()
    cache = _cache(clock)
    cache.put("alice", 1, "figures")
    clock.now += 59.9
    assert cache.get("alice", 1) == "figures"
    clock.now += 0.1
    assert cache.get("alice", 1) is None


def test_the_least_recently_used_entry_is_evicted_beyond_the_bound() -> None:
    cache = _cache(Clock(), max_entries=2)
    cache.put("a", 1, "A")
    cache.put("b", 1, "B")
    assert cache.get("a", 1) == "A"  # a is now the most recently used
    cache.put("c", 1, "C")
    assert (cache.get("a", 1), cache.get("b", 1), cache.get("c", 1)) == ("A", None, "C")
    cache.clear()
    assert len(cache) == 0


def test_lookups_are_counted_by_outcome_with_the_cache_name_only() -> None:
    cache = VersionedCache[str, str]("metric_test", max_entries=2, ttl_seconds=60)
    hits = READ_CACHE_LOOKUPS_TOTAL.labels(cache="metric_test", outcome="hit")
    misses = READ_CACHE_LOOKUPS_TOTAL.labels(cache="metric_test", outcome="miss")
    before = (hits._value.get(), misses._value.get())
    cache.get("k", 1)
    cache.put("k", 1, "v")
    cache.get("k", 1)
    assert (hits._value.get(), misses._value.get()) == (before[0] + 1, before[1] + 1)


@pytest.mark.parametrize(("max_entries", "ttl"), [(0, 60.0), (1, 0.0)])
def test_a_bound_and_a_ttl_are_required(max_entries: int, ttl: float) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        VersionedCache[str, str]("bad", max_entries=max_entries, ttl_seconds=ttl)
