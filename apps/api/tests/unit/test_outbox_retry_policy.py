"""TASK-026 / ADR-0040: the outbox backoff schedule and the dispatch handler registry."""

from datetime import timedelta

import pytest
from pbip_api.core.config import Settings
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.notifications.service import OUTBOX_HANDLERS, RetryPolicy

pytestmark = pytest.mark.unit


def test_backoff_doubles_from_the_base_and_is_capped() -> None:
    policy = RetryPolicy(max_attempts=10, base_seconds=30, max_seconds=600)
    assert [policy.delay(n).total_seconds() for n in range(1, 8)] == [
        30,
        60,
        120,
        240,
        480,
        600,
        600,
    ]


def test_backoff_never_overflows_for_large_attempt_counts() -> None:
    policy = RetryPolicy(max_attempts=20, base_seconds=3600, max_seconds=86_400)
    assert policy.delay(1_000) == timedelta(days=1)


def test_default_schedule_dead_letters_about_fifteen_minutes_after_the_first_failure() -> None:
    policy = RetryPolicy()
    waits = [policy.delay(n) for n in range(1, policy.max_attempts)]
    assert waits == [timedelta(seconds=s) for s in (30, 60, 120, 240, 480)]
    assert sum(waits, timedelta()) == timedelta(minutes=15, seconds=30)


def test_policy_defaults_equal_the_validated_settings_defaults() -> None:
    fields = Settings.model_fields
    assert RetryPolicy() == RetryPolicy(
        max_attempts=fields["outbox_max_attempts"].default,
        base_seconds=fields["outbox_retry_base_seconds"].default,
        max_seconds=fields["outbox_retry_max_seconds"].default,
    )


def test_every_enqueued_event_type_has_a_handler() -> None:
    """A type without a handler would be retried and dead-lettered, never delivered."""
    enqueued = {
        value
        for name, value in vars(notifications).items()
        if name.isupper() and isinstance(value, str) and name != "PROJECTED_LOW_BALANCE"
    }
    assert enqueued == set(OUTBOX_HANDLERS)
    # Written directly by the forecast (TASK-043), never through the outbox.
    assert notifications.PROJECTED_LOW_BALANCE not in OUTBOX_HANDLERS
