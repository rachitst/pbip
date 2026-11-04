"""Daily behavioural profile job (UC-11, ADR-0057).

For every customer whose analytics consent is on, derives sessions, engagement and product
interests from the last ``PROFILE_WINDOW_DAYS`` of clickstream (``profiling``) and stores them;
removes the profile of anyone whose consent is off (withdrawal already erases it, this is the
safety net). Runs from Celery beat, never from a request. One commit at the end.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy.orm import Session

from pbip_api.modules.analytics import profile_repository as repo
from pbip_api.modules.analytics.profiling import (
    PROFILE_WINDOW_DAYS,
    ProfileEvent,
    derive_sessions,
    score_interests,
    summarise_engagement,
)
from pbip_api.modules.customers import service as customers_service

_logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ProfileJobResult:
    profiled: int
    removed: int
    skipped: bool = False


def _store(session: Session, customer_id: UUID, events: list[ProfileEvent], now: datetime) -> None:
    repo.replace_profile(
        session,
        customer_id,
        summarise_engagement(derive_sessions(events)),
        score_interests(events, now),
        PROFILE_WINDOW_DAYS,
        now,
    )


def run_behaviour_profile_job(session: Session, now: datetime) -> ProfileJobResult:
    """Rebuild every consenting customer's profile as of ``now``. Idempotent; a run that finds
    another one in progress does nothing (advisory lock)."""
    if not repo.try_lock_profile_job(session):
        session.rollback()
        _logger.info("behaviour_profile_job_skipped", reason="another run holds the lock")
        return ProfileJobResult(profiled=0, removed=0, skipped=True)
    consenting = customers_service.analytics_consenting_customer_ids(session)
    removed = repo.delete_profiles_except(session, consenting)
    since = now - timedelta(days=PROFILE_WINDOW_DAYS)
    pending: dict[UUID, list[ProfileEvent]] = {customer_id: [] for customer_id in consenting}
    current: UUID | None = None
    for customer_id, event in repo.events_by_customer(session, consenting, since, now):
        if customer_id != current and current is not None:
            _store(session, current, pending.pop(current), now)
        current = customer_id
        pending[customer_id].append(event)
    if current is not None:
        _store(session, current, pending.pop(current), now)
    for customer_id in pending:  # consenting, but no event in the window
        _store(session, customer_id, [], now)
    session.commit()
    _logger.info("behaviour_profile_job_done", profiled=len(consenting), removed=removed)
    return ProfileJobResult(profiled=len(consenting), removed=removed)
