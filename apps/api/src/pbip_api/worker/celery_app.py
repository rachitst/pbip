"""Celery application. Tasks must be idempotent: with acks_late a task may run twice."""

from celery import Celery
from celery.schedules import crontab

from pbip_api.core.config import get_settings

_settings = get_settings()

celery_app = Celery(
    "pbip",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=["pbip_api.worker.tasks"],
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    result_expires=3600,
    beat_schedule={
        "relay-outbox": {"task": "pbip.outbox.relay", "schedule": 10.0},
        # TASK-015 / ADR-0021: recurring and scheduled transfer execution engine.
        "execute-scheduled-transfers": {
            "task": "pbip.transfers.execute_scheduled",
            "schedule": 60.0,
        },
        # TASK-052 / ADR-0011: one nightly spending job (no second materialisation job).
        "materialize-monthly-spending": {
            "task": "pbip.analytics.materialize_monthly_spending",
            "schedule": crontab(hour=2, minute=0),
        },
        # TASK-055: monthly clickstream partitions are created months ahead (idempotent).
        "ensure-clickstream-partitions": {
            "task": "pbip.events.ensure_partitions",
            "schedule": crontab(hour=1, minute=30),
        },
        # TASK-052 / ADR-0012: segments are recomputed weekly (Sunday), after the nightly job.
        "segment-customers-weekly": {
            "task": "pbip.analytics.segment_customers",
            "schedule": crontab(hour=3, minute=0, day_of_week=0),
        },
    },
)
