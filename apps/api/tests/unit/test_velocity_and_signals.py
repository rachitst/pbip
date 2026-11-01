"""TASK-034 Redis velocity windows and TASK-095 device/network identity (no database)."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fakeredis import FakeRedis, FakeServer
from pbip_api.core.velocity import VelocityCounters
from pbip_api.modules.fraud.signals import device_uuid, device_uuid_from_hash, ip_network
from redis import Redis

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


@pytest.fixture
def counters() -> VelocityCounters:
    return VelocityCounters(FakeRedis(decode_responses=True))


def test_transfer_window_slides_and_excludes_the_transfer_being_scored(
    counters: VelocityCounters,
) -> None:
    account, current = uuid4(), uuid4()
    for minutes_ago in (11, 9, 5, 1):
        counters.record_transfer(account, uuid4(), 1_000_00, T0 - timedelta(minutes=minutes_ago))
    counters.record_transfer(account, current, 7_00, T0 - timedelta(seconds=1))
    assert counters.transfers_in_window(account, T0, exclude=current) == 3  # 11 min ago is out
    assert counters.volume_in_window(account, T0, exclude=current) == 4_000_00  # all in 1 hour
    assert counters.transfers_in_window(uuid4(), T0, exclude=current) == 0  # per account


def test_volume_window_is_one_hour_and_old_entries_expire(counters: VelocityCounters) -> None:
    account = uuid4()
    counters.record_transfer(account, uuid4(), 5_00, T0 - timedelta(minutes=61))
    counters.record_transfer(account, uuid4(), 2_00, T0 - timedelta(minutes=59))
    assert counters.volume_in_window(account, T0, exclude=uuid4()) == 2_00
    ttl = counters.redis.ttl(f"fraud:vel:tx:{account}")
    assert 0 < int(ttl) <= 3_660  # the whole key expires by itself


def test_login_failures_per_ip_expire_after_24_hours(counters: VelocityCounters) -> None:
    counters.record_login_failure("203.0.113.7", uuid4(), T0 - timedelta(hours=25))
    for _ in range(3):
        counters.record_login_failure("203.0.113.7", uuid4(), T0 - timedelta(hours=1))
    assert counters.login_failures_from_ip("203.0.113.7", T0) == 3
    assert counters.login_failures_from_ip("198.51.100.9", T0) == 0
    assert 0 < int(counters.redis.ttl("fraud:vel:loginfail:203.0.113.7")) <= 24 * 3600 + 60


def test_unreachable_redis_fails_open_with_none_so_the_caller_uses_the_database() -> None:
    down = VelocityCounters(Redis(host="127.0.0.1", port=9, socket_connect_timeout=0.2))
    account = uuid4()
    assert down.record_transfer(account, uuid4(), 1_00, T0) is False
    assert down.transfers_in_window(account, T0, exclude=uuid4()) is None
    assert down.volume_in_window(account, T0, exclude=uuid4()) is None
    assert down.login_failures_from_ip("203.0.113.7", T0) is None


def test_concurrent_writers_never_lose_or_double_count_an_event() -> None:
    server = FakeServer()
    account = uuid4()

    def record(i: int) -> bool:  # one client per thread, one shared server: like API workers
        client = VelocityCounters(FakeRedis(server=server, decode_responses=True))
        return client.record_transfer(
            account, uuid4(), 100 + i, T0 - timedelta(seconds=1 + i % 300)
        )  # window is [T0-10m, T0)

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert all(pool.map(record, range(400)))
    reader = VelocityCounters(FakeRedis(server=server, decode_responses=True))
    assert reader.transfers_in_window(account, T0, exclude=uuid4()) == 400
    assert reader.volume_in_window(account, T0, exclude=uuid4()) == sum(100 + i for i in range(400))


def test_device_id_is_stable_and_matches_the_registry_hash() -> None:
    raw = "4b1c2f0e-browser-id"
    assert device_uuid(raw) == device_uuid(f"  {raw} ")
    assert device_uuid(raw) == device_uuid_from_hash(hashlib.sha256(raw.encode()).hexdigest())
    assert device_uuid(raw) != device_uuid("another-browser")
    assert device_uuid(None) is None and device_uuid("   ") is None


@pytest.mark.parametrize(
    ("ip", "network"),
    [("203.0.113.7", "203.0.113.0/24"), ("2001:db8:1:2::5", "2001:db8:1::/48"),
     ("testclient", None), (None, None), ("", None)],
)  # fmt: skip
def test_network_is_a_prefix_never_the_full_address(ip: str | None, network: str | None) -> None:
    assert ip_network(ip) == network
