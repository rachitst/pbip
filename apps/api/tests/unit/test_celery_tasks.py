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
