"""Celery tasks. Tasks must be idempotent: with acks_late a task may run twice."""

from pbip_api.core.db import session_scope
from pbip_api.modules.analytics.service import materialize_monthly_spending
from pbip_api.modules.events.partitions import ensure_monthly_partitions
from pbip_api.modules.notifications.service import relay_outbox
from pbip_api.worker.celery_app import celery_app


@celery_app.task(name="pbip.ping")
def ping() -> str:
    """Round-trip check used by the smoke test to prove broker + worker are alive."""
    return "pong"


@celery_app.task(name="pbip.outbox.relay")
def relay_outbox_task() -> int:
    """Turn committed outbox events into notifications (runs every 10 seconds via beat)."""
    with session_scope() as session:
        return relay_outbox(session)


@celery_app.task(name="pbip.analytics.materialize_monthly_spending")
def materialize_monthly_spending_task() -> int:
    """Store the closed months' per-category spend (nightly via beat; ADR-0011). Idempotent."""
    with session_scope() as session:
        return materialize_monthly_spending(session)


@celery_app.task(name="pbip.analytics.segment_customers")
def segment_customers_task() -> int:
    """Re-run the offline K-Means segmentation (weekly via beat; ADR-0012). Idempotent upserts.

    The job module is imported here, not at module level: it loads scikit-learn, which must stay out
    of every process that merely imports the task registry (the API, the beat scheduler).
    """
    from pbip_api.core.db import utcnow
    from pbip_api.modules.analytics.segmentation_job import run_offline_segmentation_job

    with session_scope() as session:
        result = run_offline_segmentation_job(session, utcnow())
    return result.total_customers_assigned


@celery_app.task(name="pbip.events.ensure_partitions")
def ensure_clickstream_partitions_task() -> list[str]:
    """Create the coming months' clickstream partitions ahead (daily via beat). Idempotent."""
    from pbip_api.core.db import utcnow

    with session_scope() as session:
        return ensure_monthly_partitions(session, utcnow().date())


@celery_app.task(name="pbip.transfers.execute_scheduled")
def execute_scheduled_transfers_task() -> dict[str, int]:
    """Execute due scheduled transfers and process missed recovery (60s beat; ADR-0021)."""
    from pbip_api.modules.payments.scheduled_service import process_due_scheduled_transfers

    return process_due_scheduled_transfers()
