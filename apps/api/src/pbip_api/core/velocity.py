"""Redis sliding-window velocity counters for fraud scoring (TASK-034).

Windows are Redis sorted sets scored by epoch seconds, so a window is a true sliding window (not a
fixed bucket). Keys:

* ``fraud:vel:tx:<account_id>``: one member per outgoing transfer (``<txn id>:<amount>``); the
  transfers in the last 10 minutes and the volume in the last hour both come from this one set.
* ``fraud:vel:loginfail:<ip>``: one member per failed sign-in from that IP, kept 24 hours.

Every write is one ``MULTI``/``EXEC`` pipeline (add, trim the expired tail, refresh the TTL), so
concurrent requests can never lose or double-count an event. Reads are single commands.

**Fail-open to the database.** Every method returns ``None`` when Redis is unreachable; the scorer
then counts from PostgreSQL instead (the ledger and ``security_events`` are the source of truth;
Redis is a fast cache of the same facts). Scoring itself never writes: the payment services record
an attempt only after their transaction has committed.
"""

from datetime import datetime, timedelta
from uuid import UUID

import structlog
from redis import Redis
from redis.exceptions import RedisError

_logger = structlog.get_logger(__name__)

TRANSFER_WINDOW = timedelta(minutes=10)
VOLUME_WINDOW = timedelta(hours=1)
LOGIN_FAILURE_WINDOW = timedelta(hours=24)


def _tx_key(account_id: UUID) -> str:
    return f"fraud:vel:tx:{account_id}"


def _login_key(ip: str) -> str:
    return f"fraud:vel:loginfail:{ip}"


class VelocityCounters:
    def __init__(self, redis_client: Redis) -> None:
        self.redis = redis_client

    def record_transfer(
        self, account_id: UUID, transaction_id: UUID, amount_minor: int, at: datetime
    ) -> bool:
        """Count one outgoing attempt. Returns False (and logs) if Redis is unavailable."""
        return self._add(_tx_key(account_id), f"{transaction_id}:{amount_minor}", at, VOLUME_WINDOW)

    def record_login_failure(self, ip: str, event_id: UUID, at: datetime) -> bool:
        return self._add(_login_key(ip), str(event_id), at, LOGIN_FAILURE_WINDOW)

    def transfers_in_window(self, account_id: UUID, at: datetime, exclude: UUID) -> int | None:
        """Attempts in the 10 minutes before ``at``, not counting ``exclude`` (the one scored)."""
        members = self._range(_tx_key(account_id), at - TRANSFER_WINDOW, at)
        if members is None:
            return None
        return sum(1 for m in members if not m.startswith(f"{exclude}:"))

    def volume_in_window(self, account_id: UUID, at: datetime, exclude: UUID) -> int | None:
        """Sum of attempt amounts (minor units) in the hour before ``at``, excluding ``exclude``."""
        members = self._range(_tx_key(account_id), at - VOLUME_WINDOW, at)
        if members is None:
            return None
        return sum(int(m.rsplit(":", 1)[1]) for m in members if not m.startswith(f"{exclude}:"))

    def login_failures_from_ip(self, ip: str, at: datetime) -> int | None:
        members = self._range(_login_key(ip), at - LOGIN_FAILURE_WINDOW, at)
        return None if members is None else len(members)

    def _add(self, key: str, member: str, at: datetime, keep: timedelta) -> bool:
        now = at.timestamp()
        try:
            with self.redis.pipeline(transaction=True) as pipe:
                pipe.zadd(key, {member: now})
                pipe.zremrangebyscore(key, "-inf", now - keep.total_seconds())
                pipe.expire(key, int(keep.total_seconds()) + 60)
                pipe.execute()
        except RedisError as exc:
            _logger.warning("velocity_counter_unavailable", op="add", error_type=type(exc).__name__)
            return False
        return True

    def _range(self, key: str, start: datetime, end: datetime) -> list[str] | None:
        try:
            # Window [start, end): "(" makes the upper bound exclusive.
            members = self.redis.zrangebyscore(key, start.timestamp(), f"({end.timestamp()}")
        except RedisError as exc:
            _logger.warning(
                "velocity_counter_unavailable", op="read", error_type=type(exc).__name__
            )
            return None
        return [str(m) for m in members]  # decode_responses=True: members are already str
