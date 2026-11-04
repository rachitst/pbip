"""Celery application. Tasks must be idempotent: with acks_late a task may run twice."""

from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, worker_process_init

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
        # TASK-015 / ADR-0023: recurring and scheduled transfer execution engine.
        "execute-scheduled-transfers": {
            "task": "pbip.transfers.execute_scheduled",
            "schedule": 60.0,
        },
        # Issue #332 / ADR-0029: pay matured fixed deposits back to their source account. Hourly,
        # so a deposit is paid within the hour after its IST maturity day starts; held ones retry.
        "pay-out-matured-deposits": {
            "task": "pbip.wealth.pay_out_matured_deposits",
            "schedule": crontab(minute=5),
        },
        # ADR-0058: fund SIP instalments, hourly at :50 (away from :05, :20 and :35), so one due
        # on an IST bank day is bought within the hour; each run is exactly-once per (SIP, day).
        "run-fund-sips": {
            "task": "pbip.wealth.run_fund_sips",
            "schedule": crontab(minute=50),
        },
        # Issue #314 / ADR-0036: decline step-ups left unanswered past STEP_UP_EXPIRY_MINUTES.
        "expire-stale-step-ups": {
            "task": "pbip.transfers.expire_stale_step_ups",
            "schedule": 60.0,
        },
        # Issue #314 / ADR-0036: delete idempotency keys past their TTL (already ignored on use).
        "purge-expired-idempotency-keys": {
            "task": "pbip.ledger.purge_expired_idempotency_keys",
            "schedule": crontab(hour=4, minute=15),
        },
        # TASK-068 follow-up #324 / ADR-0038: PSI drift of the fraud inputs, hourly at :20 (away
        # from the :05 payout run); the API's /metrics exports the stored result.
        "evaluate-feature-drift": {
            "task": "pbip.fraud.evaluate_feature_drift",
            "schedule": crontab(minute=20),
        },
        # #274 / ADR-0052: F7 deposit-side structuring monitor, hourly at :35 (away from :05/:20).
        "monitor-structuring-deposits": {
            "task": "pbip.fraud.monitor_structuring_deposits",
            "schedule": crontab(minute=35),
        },
        # TASK-026 / ADR-0040: delete processed outbox events past OUTBOX_RETENTION_DAYS.
        "purge-processed-outbox-events": {
            "task": "pbip.outbox.purge_processed",
            "schedule": crontab(hour=4, minute=30),
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


def _trace_worker_process(**_signal_kwargs: object) -> None:
    """OpenTelemetry for each worker process, after the prefork fork (ADR-0050); no-op when off."""
    from pbip_api.core.tracing import setup_celery_tracing

    setup_celery_tracing("worker")


def _trace_beat(**_signal_kwargs: object) -> None:
    """Beat publishes the scheduled tasks: tracing it puts traceparent in their headers."""
    from pbip_api.core.tracing import setup_celery_tracing

    setup_celery_tracing("beat")


worker_process_init.connect(_trace_worker_process, weak=False)
beat_init.connect(_trace_beat, weak=False)
