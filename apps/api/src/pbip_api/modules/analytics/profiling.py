"""Sessions, engagement and product interests of one customer's clickstream (UC-11, ADR-0057).

Pure functions over events already read from ``clickstream_events``, so every rule is unit-tested
exactly; the profile job (``profile_job.py``) feeds them and stores the result. The rules:

* **Session.** Events with the same ``session_id`` (one per signed-in visit, TASK-112), ordered by
  time whatever order they arrived in. A gap longer than ``INACTIVITY_TIMEOUT`` (30 minutes, the
  conventional web-analytics timeout) ends the session and the next event starts a new one. The
  duration is the last event's time minus the first's, so a one-event session lasts 0 s.
  ``session_heartbeat`` events (sent while the page is visible and in use) extend a session but are
  not interactions: they are not counted as events, and a run of heartbeats alone is no session.
* **Engagement.** Sessions, their mean duration, page views per session, interactions (events
  other than heartbeats) and the time of the last interaction.
* **Product interest.** Each interest signal scores ``weight * 0.5 ** (age_days / 14)`` for its
  product category (a signal loses half its weight every 14 days), summed per category over the
  profile window: a recommendation card opened (``product_clicked`` of a catalogue product, its
  ``product_type``) weighs 3, a loan calculator used weighs 2 (LOAN), a visit to a product page
  weighs 1 (``/loans``: LOAN; ``/wealth`` and ``/wealth/compare``: INVESTMENT). Nothing else is a
  signal; an unknown product id is ignored.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from pbip_ml.recommendations import PRODUCT_CATALOGUE, ProductType

from pbip_api.modules.events.schemas import TelemetryEventType

INACTIVITY_TIMEOUT = timedelta(minutes=30)
PROFILE_WINDOW_DAYS = 90
INTEREST_HALF_LIFE_DAYS = 14.0

CLICK_WEIGHT = 3.0
CALCULATOR_WEIGHT = 2.0
PAGE_WEIGHT = 1.0
PRODUCT_PAGES: dict[str, ProductType] = {
    "/loans": ProductType.LOAN,
    "/wealth": ProductType.INVESTMENT,
    "/wealth/compare": ProductType.INVESTMENT,
}
_PRODUCT_TYPES = {str(p.product_id): p.product_type for p in PRODUCT_CATALOGUE}
_HEARTBEAT = TelemetryEventType.SESSION_HEARTBEAT.value
_PAGE_VIEW = TelemetryEventType.PAGE_VIEW.value


@dataclass(frozen=True, slots=True)
class ProfileEvent:
    """The fields of a stored event the profile uses (never ids of accounts or free text)."""

    session_id: str
    event_type: str
    at: datetime
    page_url: str | None = None
    product_id: str | None = None


@dataclass(frozen=True, slots=True)
class DerivedSession:
    session_id: str
    start: datetime
    end: datetime
    interactions: int
    page_views: int

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


@dataclass(frozen=True, slots=True)
class Engagement:
    sessions: int
    avg_session_seconds: int
    page_views: int
    interactions: int
    last_active_at: datetime | None

    @property
    def pages_per_session(self) -> float:
        return round(self.page_views / self.sessions, 2) if self.sessions else 0.0


@dataclass(frozen=True, slots=True)
class ProductInterest:
    category: ProductType
    score: float
    signals: int
    last_signal_at: datetime


def _split(session_id: str, events: list[ProfileEvent], timeout: timedelta) -> list[DerivedSession]:
    runs: list[list[ProfileEvent]] = []
    for event in sorted(events, key=lambda e: e.at):
        if runs and event.at - runs[-1][-1].at <= timeout:
            runs[-1].append(event)
        else:
            runs.append([event])
    sessions = []
    for run in runs:
        interactions = sum(1 for e in run if e.event_type != _HEARTBEAT)
        if interactions == 0:
            continue
        page_views = sum(1 for e in run if e.event_type == _PAGE_VIEW)
        sessions.append(DerivedSession(session_id, run[0].at, run[-1].at, interactions, page_views))
    return sessions


def derive_sessions(
    events: Iterable[ProfileEvent], timeout: timedelta = INACTIVITY_TIMEOUT
) -> list[DerivedSession]:
    """Sessions of one customer's events (rules in the module docstring), oldest first."""
    by_id: dict[str, list[ProfileEvent]] = defaultdict(list)
    for event in events:
        by_id[event.session_id].append(event)
    sessions = [s for sid, evs in by_id.items() for s in _split(sid, evs, timeout)]
    return sorted(sessions, key=lambda s: (s.start, s.session_id))


def summarise_engagement(sessions: list[DerivedSession]) -> Engagement:
    if not sessions:
        return Engagement(0, 0, 0, 0, None)
    return Engagement(
        sessions=len(sessions),
        avg_session_seconds=round(sum(s.seconds for s in sessions) / len(sessions)),
        page_views=sum(s.page_views for s in sessions),
        interactions=sum(s.interactions for s in sessions),
        last_active_at=max(s.end for s in sessions),
    )


def interest_signal(event: ProfileEvent) -> tuple[ProductType, float] | None:
    """The category and weight an event signals, or None when it says nothing about products."""
    if event.event_type == TelemetryEventType.PRODUCT_CLICKED.value:
        category = _PRODUCT_TYPES.get(event.product_id or "")
        return (category, CLICK_WEIGHT) if category is not None else None
    if event.event_type == TelemetryEventType.LOAN_CALCULATOR_USED.value:
        return ProductType.LOAN, CALCULATOR_WEIGHT
    if event.event_type == _PAGE_VIEW and event.page_url in PRODUCT_PAGES:
        return PRODUCT_PAGES[event.page_url], PAGE_WEIGHT
    return None


def score_interests(
    events: Iterable[ProfileEvent],
    now: datetime,
    window: timedelta = timedelta(days=PROFILE_WINDOW_DAYS),
    half_life_days: float = INTEREST_HALF_LIFE_DAYS,
) -> list[ProductInterest]:
    """Recency-weighted interest per category, highest first (ties: category name).

    Signals older than ``window`` are ignored; one dated after ``now`` (clock skew) counts as
    happening now, never more.
    """
    scores: dict[ProductType, float] = defaultdict(float)
    counts: dict[ProductType, int] = defaultdict(int)
    latest: dict[ProductType, datetime] = {}
    for event in events:
        signal = interest_signal(event)
        if signal is None or event.at < now - window:
            continue
        category, weight = signal
        age_days = max((now - event.at).total_seconds(), 0.0) / 86_400
        scores[category] += weight * 0.5 ** (age_days / half_life_days)
        counts[category] += 1
        latest[category] = max(latest.get(category, event.at), event.at)
    interests = [
        ProductInterest(category, round(scores[category], 3), counts[category], latest[category])
        for category in scores
    ]
    return sorted(interests, key=lambda i: (-i.score, i.category.value))
