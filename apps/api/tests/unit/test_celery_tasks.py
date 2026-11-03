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
