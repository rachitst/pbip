import pytest
from celery.schedules import crontab
from pbip_api.worker.celery_app import celery_app
from pbip_api.worker.tasks import ping

pytestmark = pytest.mark.unit


def test_ping_task_returns_pong() -> None:
    assert ping() == "pong"


def test_beat_runs_one_nightly_spending_job_and_a_weekly_segmentation_job() -> None:
    """TASK-052: a single nightly materialisation at 02:00 UTC (no duplicate job) and the weekly
    segmentation run, each pointing at a registered task."""
    schedule = celery_app.conf.beat_schedule
    spending = [
        e for e in schedule.values() if e["task"] == "pbip.analytics.materialize_monthly_spending"
    ]
    assert len(spending) == 1
    assert spending[0]["schedule"] == crontab(hour=2, minute=0)
    segments = schedule["segment-customers-weekly"]
    assert segments["schedule"] == crontab(hour=3, minute=0, day_of_week=0)
    registered = set(celery_app.tasks)
    for entry in schedule.values():
        assert entry["task"] in registered, entry["task"]


def test_beat_runs_scheduled_transfers_job() -> None:
    schedule = celery_app.conf.beat_schedule
    entry = schedule["execute-scheduled-transfers"]
    assert entry["task"] == "pbip.transfers.execute_scheduled"
    assert entry["schedule"] == 60.0


def test_beat_pays_out_matured_deposits_hourly_through_a_registered_task() -> None:
    """Issue #332 / ADR-0029: one hourly entry on the existing beat, pointing at a real task."""
    schedule = celery_app.conf.beat_schedule
    entries = [e for e in schedule.values() if e["task"] == "pbip.wealth.pay_out_matured_deposits"]
    assert len(entries) == 1
    assert entries[0]["schedule"] == crontab(minute=5)
    assert "pbip.wealth.pay_out_matured_deposits" in celery_app.tasks


def test_beat_runs_fund_sips_hourly_through_a_registered_task() -> None:
    """ADR-0058 section 7: one hourly entry at :50 on the existing beat, pointing at a real task."""
    schedule = celery_app.conf.beat_schedule
    entries = [e for e in schedule.values() if e["task"] == "pbip.wealth.run_fund_sips"]
    assert len(entries) == 1
    assert entries[0]["schedule"] == crontab(minute=50)
    assert "pbip.wealth.run_fund_sips" in celery_app.tasks


def test_beat_expires_stale_step_ups_every_minute_and_purges_keys_daily() -> None:
    """Issue #314 / ADR-0036: two entries on the existing beat, each pointing at a real task."""
    schedule = celery_app.conf.beat_schedule
    expiry = [e for e in schedule.values() if e["task"] == "pbip.transfers.expire_stale_step_ups"]
    purge = [
        e for e in schedule.values() if e["task"] == "pbip.ledger.purge_expired_idempotency_keys"
    ]
    assert len(expiry) == 1 and expiry[0]["schedule"] == 60.0
    assert len(purge) == 1 and purge[0]["schedule"] == crontab(hour=4, minute=15)
    assert {
        "pbip.transfers.expire_stale_step_ups",
        "pbip.ledger.purge_expired_idempotency_keys",
    } <= (set(celery_app.tasks))


def test_beat_evaluates_feature_drift_hourly_through_a_registered_task() -> None:
    """#324 / ADR-0038: one hourly PSI entry on the existing beat, inside the 3-hour result TTL."""
    from pbip_api.modules.fraud.drift import RESULT_TTL

    schedule = celery_app.conf.beat_schedule
    entries = [e for e in schedule.values() if e["task"] == "pbip.fraud.evaluate_feature_drift"]
    assert len(entries) == 1
    assert entries[0]["schedule"] == crontab(minute=20)
    assert "pbip.fraud.evaluate_feature_drift" in celery_app.tasks
    assert RESULT_TTL.total_seconds() >= 2 * 3600  # one missed run never drops the gauge


def test_beat_relays_the_outbox_every_ten_seconds_and_purges_it_daily() -> None:
    """TASK-026 / ADR-0040: the relay keeps its 10 s cadence; one daily purge after the key purge."""
    schedule = celery_app.conf.beat_schedule
    relay = [e for e in schedule.values() if e["task"] == "pbip.outbox.relay"]
    purge = [e for e in schedule.values() if e["task"] == "pbip.outbox.purge_processed"]
    assert len(relay) == 1 and relay[0]["schedule"] == 10.0
    assert len(purge) == 1 and purge[0]["schedule"] == crontab(hour=4, minute=30)
    assert {"pbip.outbox.relay", "pbip.outbox.purge_processed"} <= set(celery_app.tasks)


def test_beat_monitors_structured_deposits_hourly_through_a_registered_task() -> None:
    """#274 / ADR-0052: one hourly entry at :35, away from the :05 payout and :20 drift runs."""
    schedule = celery_app.conf.beat_schedule
    entries = [
        e for e in schedule.values() if e["task"] == "pbip.fraud.monitor_structuring_deposits"
    ]
    assert len(entries) == 1
    assert entries[0]["schedule"] == crontab(minute=35)
    assert "pbip.fraud.monitor_structuring_deposits" in celery_app.tasks
