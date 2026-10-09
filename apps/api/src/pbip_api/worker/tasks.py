"""Celery tasks. Tasks must be idempotent: with acks_late a task may run twice."""

from pbip_api.core.db import session_scope
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
