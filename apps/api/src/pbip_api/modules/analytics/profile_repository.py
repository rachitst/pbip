"""Storage of behavioural profiles and product interests (UC-11, ADR-0057).

Reads the consenting customers' clickstream and writes ``customer_behaviour_profiles`` and
``customer_product_interests``; nothing else. Kept apart from ``repository.py``, which serves the
ledger-based analytics.
"""

from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pbip_api.modules.analytics.models import CustomerBehaviourProfile, CustomerProductInterest
from pbip_api.modules.analytics.profiling import Engagement, ProductInterest, ProfileEvent
from pbip_api.modules.events.models import ClickstreamEvent

# Advisory lock key of the profile job ("BEHPROF" in ASCII), so two runs never interleave.
PROFILE_JOB_LOCK_KEY = 0x42454850524F46


@dataclass(frozen=True)
class StoredProfile:
    profile: CustomerBehaviourProfile
    interests: list[CustomerProductInterest]


def try_lock_profile_job(session: Session) -> bool:
    """Transaction-scoped; False when another run holds it (released at commit or rollback)."""
    return bool(
        session.execute(select(func.pg_try_advisory_xact_lock(PROFILE_JOB_LOCK_KEY))).scalar_one()
    )


def events_by_customer(
    session: Session, customer_ids: Collection[UUID], since: datetime, until: datetime
) -> Iterator[tuple[UUID, ProfileEvent]]:
    """The customers' events in [since, until), customer by customer, streamed in chunks."""
    if not customer_ids:
        return
    rows = session.execute(
        select(
            ClickstreamEvent.customer_id,
            ClickstreamEvent.session_id,
            ClickstreamEvent.event_type,
            ClickstreamEvent.event_timestamp,
            ClickstreamEvent.page_url,
            ClickstreamEvent.properties["product_id"].astext,
        )
        .where(
            ClickstreamEvent.customer_id.in_(list(customer_ids)),
            ClickstreamEvent.event_timestamp >= since,
            ClickstreamEvent.event_timestamp < until,
        )
        .order_by(ClickstreamEvent.customer_id, ClickstreamEvent.event_timestamp)
        .execution_options(yield_per=2000)
    )
    for customer_id, session_id, event_type, at, page_url, product_id in rows:
        yield customer_id, ProfileEvent(session_id, event_type, at, page_url, product_id)


def replace_profile(
    session: Session,
    customer_id: UUID,
    engagement: Engagement,
    interests: list[ProductInterest],
    window_days: int,
    now: datetime,
) -> None:
    """Upsert the customer's profile and replace their interest rows (safe to re-run)."""
    values = {
        "customer_id": customer_id,
        "window_days": window_days,
        "sessions": engagement.sessions,
        "avg_session_seconds": engagement.avg_session_seconds,
        "page_views": engagement.page_views,
        "interactions": engagement.interactions,
        "last_active_at": engagement.last_active_at,
        "computed_at": now,
        "updated_at": now,
    }
    stmt = insert(CustomerBehaviourProfile).values(values)
    session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_customer_behaviour_profiles_customer_id",
            set_={key: getattr(stmt.excluded, key) for key in values if key != "customer_id"},
        )
    )
    session.execute(
        delete(CustomerProductInterest).where(CustomerProductInterest.customer_id == customer_id)
    )
    if interests:
        session.execute(
            insert(CustomerProductInterest),
            [
                {
                    "customer_id": customer_id,
                    "category": i.category.value,
                    "score": i.score,
                    "signals": i.signals,
                    "last_signal_at": i.last_signal_at,
                    "computed_at": now,
                }
                for i in interests
            ],
        )


def delete_profile(session: Session, customer_id: UUID) -> bool:
    """Remove one customer's profile and interests; True when there was a profile."""
    session.execute(
        delete(CustomerProductInterest).where(CustomerProductInterest.customer_id == customer_id)
    )
    deleted = session.execute(
        delete(CustomerBehaviourProfile)
        .where(CustomerBehaviourProfile.customer_id == customer_id)
        .returning(CustomerBehaviourProfile.id)
    ).all()
    return bool(deleted)


def delete_profiles_except(session: Session, keep: Collection[UUID]) -> int:
    """Remove the profiles and interests of every customer not in ``keep``; how many profiles."""
    keep_list = list(keep)
    session.execute(
        delete(CustomerProductInterest).where(CustomerProductInterest.customer_id.not_in(keep_list))
    )
    return len(
        session.execute(
            delete(CustomerBehaviourProfile)
            .where(CustomerBehaviourProfile.customer_id.not_in(keep_list))
            .returning(CustomerBehaviourProfile.id)
        ).all()
    )


def get_profile(session: Session, customer_id: UUID) -> StoredProfile | None:
    profile = session.execute(
        select(CustomerBehaviourProfile).where(CustomerBehaviourProfile.customer_id == customer_id)
    ).scalar_one_or_none()
    if profile is None:
        return None
    interests = session.scalars(
        select(CustomerProductInterest)
        .where(CustomerProductInterest.customer_id == customer_id)
        .order_by(CustomerProductInterest.score.desc(), CustomerProductInterest.category)
    ).all()
    return StoredProfile(profile, list(interests))
