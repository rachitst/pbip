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
        "materialize-monthly-spending": {
            "task": "pbip.analytics.materialize_monthly_spending",
            "schedule": crontab(hour=2, minute=30),
        },
    },
)
