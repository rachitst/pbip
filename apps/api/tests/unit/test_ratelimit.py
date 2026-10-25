from typing import Any

import pytest
from fakeredis import FakeRedis
from pbip_api.core.ratelimit import clear_rate_limits, rate_limit_key
from redis.exceptions import ConnectionError as RedisConnectionError

pytestmark = pytest.mark.unit


def test_key_format_is_shared_by_limiter_and_clearing() -> None:
    assert rate_limit_key("login", "10.0.0.1") == "rl:login:10.0.0.1"


def test_clear_removes_only_the_requested_counters() -> None:
    redis = FakeRedis(decode_responses=True)
    for key in ("rl:login:1.1.1.1", "rl:password_change:1.1.1.1", "rl:login:9.9.9.9"):
        redis.set(key, 3)
    assert clear_rate_limits(redis, ["login", "password_change"], ["1.1.1.1", "2.2.2.2"]) == 2
    assert redis.get("rl:login:9.9.9.9") == "3"  # another client's window is untouched


def test_clear_with_no_ips_is_a_no_op() -> None:
    assert clear_rate_limits(FakeRedis(), ["login"], []) == 0


class _Down(FakeRedis):
    def delete(self, *_names: Any) -> int:
        raise RedisConnectionError("down")


def test_clear_reports_none_when_redis_is_unreachable() -> None:
    assert clear_rate_limits(_Down(), ["login"], ["1.1.1.1"]) is None
