"""IMPOSSIBLE_TRAVEL (scenario F4, ADR-0034): two located events too far apart for the time between.

The current payment's point comes from ``TransactionContext.geo_lat``/``geo_lon`` (filled by the
payments and bills services from the DB-IP City Lite database). The previous point is the most
recent located event of the same customer: a sign-in or token refresh (``security_events``, its IP
located now) or an earlier scored payment (``fraud_events`` snapshot). The implied speed is the
great-circle distance over the elapsed time.

The points are where DB-IP places an address block, so the rule needs BOTH a distance above a floor
(to ignore placement noise inside a region) and a speed above an airliner's. Anything unknown
(no database, private IP, no earlier located event) records ``location_unknown`` and adds nothing.
Side-effect free: it only reads.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pbip_ml.fraud.contract import FraudReason, ReasonSource, TransactionContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings
from pbip_api.core.geoip import GeoPoint, LocationResolver, NoLocationResolver
from pbip_api.modules.auth.models import SecurityEvent, SecurityEventType
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.fraud.models import FraudEvent

IMPOSSIBLE_TRAVEL_POINTS = 35  # alone: STEP_UP (30-59); with another rule it can reach REVIEW
EARTH_RADIUS_KM = 6371.0088  # IUGG mean radius
# No two points on Earth are further apart than half a great circle, so an event older than
# HALF_CIRCUMFERENCE_KM / max speed can never imply a speed above the threshold: it is not read.
HALF_CIRCUMFERENCE_KM = math.pi * EARTH_RADIUS_KM
PRIOR_EVENTS_SCANNED = 10  # per source; bounds the work (and lookups) per payment
_LOCATED_EVENTS = (SecurityEventType.LOGIN_SUCCESS, SecurityEventType.TOKEN_REFRESHED)
_EVENT_LABEL = {
    SecurityEventType.LOGIN_SUCCESS: "Sign-in",
    SecurityEventType.TOKEN_REFRESHED: "Session refresh",
}

Features = dict[str, float | int | str | bool | None]


@dataclass(frozen=True, slots=True)
class TravelPolicy:
    locations: LocationResolver
    max_speed_kmh: float
    min_distance_km: float

    @classmethod
    def from_settings(cls, settings: Settings, locations: LocationResolver) -> "TravelPolicy":
        return cls(
            locations, settings.fraud_travel_max_speed_kmh, settings.fraud_travel_min_distance_km
        )

    @classmethod
    def disabled(cls) -> "TravelPolicy":
        """No city database: every payment records ``location_unknown`` and the rule is silent."""
        defaults = Settings.model_fields
        return cls(
            NoLocationResolver(),
            float(defaults["fraud_travel_max_speed_kmh"].default),
            float(defaults["fraud_travel_min_distance_km"].default),
        )

    @property
    def lookback(self) -> timedelta:
        return timedelta(hours=HALF_CIRCUMFERENCE_KM / self.max_speed_kmh)


@dataclass(frozen=True, slots=True)
class PriorLocation:
    label: str  # "Sign-in", "Session refresh" or "Payment"
    at: datetime
    point: GeoPoint


@dataclass(frozen=True, slots=True)
class Travel:
    distance_km: float
    elapsed_seconds: float
    speed_kmh: float  # math.inf when no time passed

    def triggers(self, policy: TravelPolicy) -> bool:
        """Strictly above both thresholds: exactly at the speed limit is plausible."""
        return self.distance_km > policy.min_distance_km and self.speed_kmh > policy.max_speed_kmh


def great_circle_km(a: GeoPoint, b: GeoPoint) -> float:
    """Haversine distance on a sphere of the mean Earth radius."""
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat = lat2 - lat1
    dlon = math.radians(b.longitude - a.longitude)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def measure(prior: PriorLocation, current: GeoPoint, at: datetime) -> Travel:
    distance = great_circle_km(prior.point, current)
    elapsed = max(0.0, (at - prior.at).total_seconds())  # clock skew never goes negative
    speed = distance / (elapsed / 3600) if elapsed > 0 else (math.inf if distance > 0 else 0.0)
    return Travel(distance, elapsed, speed)


def impossible_travel(
    session: Session, context: TransactionContext, policy: TravelPolicy
) -> tuple[FraudReason | None, Features]:
    """The rule's reason (or None) and its part of the feature snapshot."""
    if context.geo_lat is None or context.geo_lon is None:
        return None, {"location_unknown": True}
    current = GeoPoint(context.geo_lat, context.geo_lon, context.ip_country)
    prior = most_recent_prior_location(session, context, policy)
    if prior is None:
        return None, {"location_unknown": True}
    travel = measure(prior, current, context.occurred_at)
    features: Features = {
        "location_unknown": False,
        "travel_prior_event": prior.label,
        "travel_prior_country": prior.point.country,
        "travel_prior_city": prior.point.city,
        "travel_prior_lat": prior.point.latitude,
        "travel_prior_lon": prior.point.longitude,
        "travel_distance_km": round(travel.distance_km, 1),
        "travel_elapsed_minutes": round(travel.elapsed_seconds / 60, 1),
        # JSON has no infinity: an instantaneous jump is stored as null with elapsed 0
        "travel_speed_kmh": round(travel.speed_kmh, 1) if math.isfinite(travel.speed_kmh) else None,
    }
    if not travel.triggers(policy):
        return None, features
    return _reason(prior, current, context.occurred_at, travel), features


def most_recent_prior_location(
    session: Session, context: TransactionContext, policy: TravelPolicy
) -> PriorLocation | None:
    """Most recent sign-in, refresh or earlier payment of this customer that has a location."""
    since = context.occurred_at - policy.lookback
    candidates = (
        _prior_session(session, context, since, policy.locations),
        _prior_payment(session, context, since),
    )
    located = [c for c in candidates if c is not None]
    return max(located, key=lambda c: c.at, default=None)


def _prior_session(
    session: Session, context: TransactionContext, since: datetime, locations: LocationResolver
) -> PriorLocation | None:
    """The newest located sign-in or refresh (its IP is located now, with today's database)."""
    user_id = select(Customer.user_id).where(Customer.id == context.customer_id).scalar_subquery()
    rows = session.execute(
        select(SecurityEvent.event_type, SecurityEvent.ip, SecurityEvent.created_at)
        .where(
            SecurityEvent.user_id == user_id,
            SecurityEvent.event_type.in_(_LOCATED_EVENTS),
            SecurityEvent.ip.is_not(None),
            SecurityEvent.created_at >= since,
            SecurityEvent.created_at <= context.occurred_at,
        )
        .order_by(SecurityEvent.created_at.desc())
        .limit(PRIOR_EVENTS_SCANNED)
    ).all()
    for event_type, ip, at in rows:
        point = locations.locate(ip)
        if point is not None:
            return PriorLocation(_EVENT_LABEL[event_type], at, point)
    return None


def _prior_payment(
    session: Session, context: TransactionContext, since: datetime
) -> PriorLocation | None:
    """The newest earlier payment whose snapshot kept a location."""
    lat = FraudEvent.features["geo_lat"].as_float()
    lon = FraudEvent.features["geo_lon"].as_float()
    row = session.execute(
        select(lat, lon, FraudEvent.features["ip_country"].astext, FraudEvent.created_at)
        .where(
            FraudEvent.customer_id == context.customer_id,
            FraudEvent.transaction_id != context.transaction_id,
            FraudEvent.created_at >= since,
            FraudEvent.created_at <= context.occurred_at,
            lat.is_not(None),
            lon.is_not(None),
        )
        .order_by(FraudEvent.created_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return PriorLocation("Payment", row[3], GeoPoint(row[0], row[1], row[2]))


def _reason(prior: PriorLocation, current: GeoPoint, at: datetime, travel: Travel) -> FraudReason:
    where = _place(prior.point)
    minutes = travel.elapsed_seconds / 60
    speed = (
        f"{travel.speed_kmh:,.0f} km/h"
        if math.isfinite(travel.speed_kmh)
        else "no time between them"
    )
    message = (
        f"{prior.label} from {where} at {prior.at.astimezone(UTC):%H:%M} UTC, this payment from "
        f"{current.country or 'an unknown country'} at {at.astimezone(UTC):%H:%M} UTC: "
        f"{travel.distance_km:,.0f} km in {minutes:,.0f} min ({speed}; approximate, ADR-0034)"
    )
    return FraudReason(
        code="IMPOSSIBLE_TRAVEL",
        message=message[:240],
        source=ReasonSource.RULE,
        contribution=IMPOSSIBLE_TRAVEL_POINTS,
    )


def _place(point: GeoPoint) -> str:
    country = point.country or "an unknown country"
    return f"{country} ({point.city})" if point.city else country
