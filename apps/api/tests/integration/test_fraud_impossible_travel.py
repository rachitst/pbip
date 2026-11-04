"""IMPOSSIBLE_TRAVEL (scenario F4, ADR-0034) against real PostgreSQL.

The city database is replaced by an in-memory MMDB reader (no file, no network), behind the real
``MmdbLocationResolver`` so private and malformed addresses behave exactly as in production. The
"now" of each scoring call is the context's ``occurred_at``, so elapsed times are exact.
"""

import math
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import utcnow
from pbip_api.core.geoip import GeoPoint, MmdbLocationResolver
from pbip_api.modules.auth.dependencies import get_request_meta
from pbip_api.modules.auth.models import SecurityEvent, SecurityEventType
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.fraud import service as fraud_service
from pbip_api.modules.fraud.baseline import BaselineRulesScorer
from pbip_api.modules.fraud.dependencies import get_location_resolver
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.fraud.travel import PRIOR_EVENTS_SCANNED, TravelPolicy
from pbip_ml.fraud.contract import FraudDecision, FraudScoreResult, TransactionContext
from pbip_test_support import Actor, age_payee, count_statements
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

LONDON_IP, MUMBAI_IP, PUNE_IP = "81.2.69.142", "49.36.0.1", "49.36.0.2"
MUMBAI = (19.0760, 72.8777)
RECORDS: dict[str, object] = {
    LONDON_IP: {
        "city": {"names": {"en": "London"}},
        "country": {"iso_code": "GB"},
        "location": {"latitude": 51.5143, "longitude": -0.0912},
    },
    MUMBAI_IP: {
        "city": {"names": {"en": "Mumbai"}},
        "country": {"iso_code": "IN"},
        "location": {"latitude": MUMBAI[0], "longitude": MUMBAI[1]},
    },
    PUNE_IP: {  # about 120 km from Mumbai: below the 500 km floor
        "city": {"names": {"en": "Pune"}},
        "country": {"iso_code": "IN"},
        "location": {"latitude": 18.5204, "longitude": 73.8567},
    },
}


class _Reader:
    def get(self, ip_address: str) -> object:
        return RECORDS.get(ip_address)


LOCATIONS = MmdbLocationResolver(_Reader())
POLICY = TravelPolicy(LOCATIONS, max_speed_kmh=900, min_distance_km=500)


def _session_event(
    db: Session, actor: Actor, ip: str | None, at: datetime, kind: SecurityEventType
) -> None:
    db.add(SecurityEvent(user_id=actor.user_id, event_type=kind, ip=ip, created_at=at))
    db.commit()


def _login(db: Session, actor: Actor, ip: str | None, at: datetime) -> None:
    _session_event(db, actor, ip, at, SecurityEventType.LOGIN_SUCCESS)


def _score(
    db: Session, actor: Actor, at: datetime, here: tuple[float, float] | None = MUMBAI
) -> FraudScoreResult:
    context = TransactionContext(
        transaction_id=uuid.uuid4(),
        customer_id=actor.customer_id,
        account_id=actor.account_id,
        amount_minor=1_000,
        currency="INR",
        occurred_at=at,
        ip_country="IN" if here else None,
        geo_lat=here[0] if here else None,
        geo_lon=here[1] if here else None,
    )
    return BaselineRulesScorer(db, None, POLICY).score(context)


def _codes(result: FraudScoreResult) -> list[str]:
    return [r.code for r in result.reasons]


def test_sign_in_in_london_then_payment_from_mumbai_two_hours_later_is_impossible(
    make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer()  # its own sign-in came from "testclient": never located
    now = utcnow()
    _login(db, alice, LONDON_IP, now - timedelta(hours=2))

    result = _score(db, alice, now)

    assert _codes(result) == ["IMPOSSIBLE_TRAVEL"]
    assert result.risk_score == 35 and result.decision == FraudDecision.STEP_UP  # alone: STEP_UP
    message = result.reasons[0].message
    assert "Sign-in from GB (London)" in message and "this payment from IN" in message
    assert "km in 120 min" in message and "km/h" in message
    features = result.features
    assert features["location_unknown"] is False
    assert features["travel_prior_event"] == "Sign-in"
    assert features["travel_prior_country"] == "GB" and features["travel_prior_city"] == "London"
    assert features["travel_elapsed_minutes"] == 120.0
    distance = features["travel_distance_km"]
    assert isinstance(distance, float) and 7_150 < distance < 7_250
    assert features["travel_speed_kmh"] == pytest.approx(distance / 2, abs=0.1)
    assert f"{distance:,.0f} km" in message
    assert (features["geo_lat"], features["geo_lon"]) == MUMBAI


def test_the_same_two_places_fourteen_hours_apart_are_plausible(
    make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer()
    now = utcnow()
    _login(db, alice, LONDON_IP, now - timedelta(hours=14))

    result = _score(db, alice, now)

    assert "IMPOSSIBLE_TRAVEL" not in _codes(result)
    assert result.features["location_unknown"] is False
    speed = result.features["travel_speed_kmh"]
    assert isinstance(speed, float) and speed < 900


def test_a_nearby_city_five_minutes_earlier_is_below_the_distance_floor(
    make_customer: Callable[..., Actor], db: Session
) -> None:
    """About 120 km in 5 minutes is over 900 km/h, but placement noise, not travel."""
    alice = make_customer()
    now = utcnow()
    _session_event(
        db, alice, PUNE_IP, now - timedelta(minutes=5), SecurityEventType.TOKEN_REFRESHED
    )

    result = _score(db, alice, now)

    assert "IMPOSSIBLE_TRAVEL" not in _codes(result)
    speed, distance = result.features["travel_speed_kmh"], result.features["travel_distance_km"]
    assert isinstance(speed, float) and speed > 900
    assert isinstance(distance, float) and distance < 500
    assert result.features["travel_prior_event"] == "Session refresh"


def test_zero_elapsed_time_triggers_without_a_division_error(
    make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer()
    now = utcnow()
    _login(db, alice, LONDON_IP, now)

    result = _score(db, alice, now)

    assert "IMPOSSIBLE_TRAVEL" in _codes(result)
    assert result.features["travel_elapsed_minutes"] == 0.0
    assert result.features["travel_speed_kmh"] is None  # infinite: JSON has no infinity
    assert "no time between them" in result.reasons[0].message


def test_most_recent_located_event_wins_over_an_older_one(
    make_customer: Callable[..., Actor], db: Session
) -> None:
    """London 3 h ago, then Mumbai 1 h ago: the payment from Mumbai compares with Mumbai."""
    alice = make_customer()
    now = utcnow()
    _login(db, alice, LONDON_IP, now - timedelta(hours=3))
    _login(db, alice, MUMBAI_IP, now - timedelta(hours=1))
    _login(db, alice, "10.0.0.7", now - timedelta(minutes=10))  # newer but private: skipped

    result = _score(db, alice, now)

    assert "IMPOSSIBLE_TRAVEL" not in _codes(result)
    assert result.features["travel_prior_city"] == "Mumbai"
    assert result.features["travel_distance_km"] == 0.0


def test_events_older_than_the_farthest_possible_trip_are_ignored(
    make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer()
    now = utcnow()
    _login(db, alice, LONDON_IP, now - timedelta(hours=23))  # lookback at 900 km/h is 22.2 h

    result = _score(db, alice, now)

    assert result.features["location_unknown"] is True
    assert "IMPOSSIBLE_TRAVEL" not in _codes(result)


@pytest.mark.parametrize(
    ("prior_ip", "here"),
    [
        (None, MUMBAI),  # first event of a new customer: no earlier located event
        ("10.0.0.7", MUMBAI),  # earlier sign-in from a private address
        ("203.0.113.9", MUMBAI),  # earlier sign-in the database cannot place
        (LONDON_IP, None),  # this payment's IP is private or unknown
    ],
)
def test_missing_location_records_location_unknown_and_adds_nothing(
    make_customer: Callable[..., Actor],
    db: Session,
    prior_ip: str | None,
    here: tuple[float, float] | None,
) -> None:
    alice = make_customer()
    now = utcnow()
    if prior_ip is not None:
        _login(db, alice, prior_ip, now - timedelta(hours=1))

    result = _score(db, alice, now, here)

    assert result.features["location_unknown"] is True
    assert result.risk_score == 0 and result.decision == FraudDecision.ALLOW
    assert not any(key.startswith("travel_") for key in result.features)


def test_without_a_city_database_the_rule_never_scores(
    make_customer: Callable[..., Actor], db: Session
) -> None:
    """The default scorer (no GEOIP_CITY_DB_PATH): the sign-in cannot be located, so nothing."""
    alice = make_customer()
    now = utcnow()
    _login(db, alice, LONDON_IP, now - timedelta(minutes=30))
    context = TransactionContext(
        transaction_id=uuid.uuid4(),
        customer_id=alice.customer_id,
        account_id=alice.account_id,
        amount_minor=1_000,
        currency="INR",
        occurred_at=now,
        geo_lat=MUMBAI[0],
        geo_lon=MUMBAI[1],
    )
    result = BaselineRulesScorer(db).score(context)
    assert result.features["location_unknown"] is True and result.risk_score == 0


class _CountingResolver(MmdbLocationResolver):
    def __init__(self) -> None:
        super().__init__(_Reader())
        self.lookups = 0

    def locate(self, ip: str | None) -> GeoPoint | None:
        self.lookups += 1
        return super().locate(ip)


def _scoring_cost(db: Session, actor: Actor, at: datetime) -> tuple[FraudScoreResult, int, int]:
    """(result, SQL statements, GeoIP lookups) for one scoring with the travel rule on."""
    resolver = _CountingResolver()
    policy = TravelPolicy(resolver, max_speed_kmh=900, min_distance_km=500)
    context = TransactionContext(
        transaction_id=uuid.uuid4(),
        customer_id=actor.customer_id,
        account_id=actor.account_id,
        amount_minor=1_000,
        currency="INR",
        occurred_at=at,
        ip_country="IN",
        geo_lat=MUMBAI[0],
        geo_lon=MUMBAI[1],
    )
    bind = db.get_bind()
    assert isinstance(bind, Engine)
    with count_statements(bind) as sent:
        result = BaselineRulesScorer(db, None, policy).score(context)
    return result, len(sent), resolver.lookups


def test_scoring_with_the_travel_lookup_stays_fast(
    make_customer: Callable[..., Actor], db: Session
) -> None:
    """p95 < 100 ms (AGENTS.md section 8) rests on bounded work, asserted here as cost, not time.

    However long the sign-in history, one scoring sends the same number of SQL statements and
    makes at most PRIOR_EVENTS_SCANNED GeoIP lookups. A wall-clock p95 failed under load (#317);
    the real latency is measured by scripts/benchmark_fraud_latency.py.
    """
    alice = make_customer()
    now = utcnow()
    _login(db, alice, "10.0.0.7", now - timedelta(minutes=1))
    _, few_queries, few_lookups = _scoring_cost(db, alice, now)
    for minutes in range(2, 13):  # more than PRIOR_EVENTS_SCANNED rows, most not located
        _login(db, alice, "10.0.0.7", now - timedelta(minutes=minutes))
    _login(db, alice, LONDON_IP, now - timedelta(minutes=30))

    result, queries, lookups = _scoring_cost(db, alice, now)
    assert result.features["location_unknown"] is True  # the 10 newest are private
    assert few_lookups < lookups <= PRIOR_EVENTS_SCANNED  # capped, however long the history
    assert queries == few_queries, "travel scoring queries grow with the sign-in history"


@pytest.fixture
def located_transfers(app: FastAPI) -> Callable[[str], None]:
    app.dependency_overrides[get_location_resolver] = lambda: LOCATIONS

    def from_ip(ip: str) -> None:
        app.dependency_overrides[get_request_meta] = lambda: RequestMeta(ip=ip)

    return from_ip


def _transfer(client: TestClient, alice: Actor, payee: dict[str, Any]) -> uuid.UUID:
    response = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={"from_account_id": str(alice.account_id), "amount_minor": 1_000,
              "payee_id": payee["id"]},
    )  # fmt: skip
    assert response.status_code in (201, 202), response.text
    body = response.json()
    assert "reasons" not in body and "risk_score" not in body  # customers never see why
    return uuid.UUID(body["id"])


def test_transfer_flow_fills_the_location_and_persists_the_travel_snapshot(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    db: Session,
    located_transfers: Callable[[str], None],
) -> None:
    """Sign-in from London 2 h ago, transfer from Mumbai, then one from London a moment later
    (the earlier transfer is now the previous located event)."""
    alice, bob = make_customer(funds=1_000_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    _login(db, alice, LONDON_IP, utcnow() - timedelta(hours=2))

    located_transfers(MUMBAI_IP)
    from_mumbai = _transfer(client, alice, payee)
    located_transfers(LONDON_IP)
    from_london = _transfer(client, alice, payee)

    events = {
        e.transaction_id: e
        for e in db.execute(
            select(FraudEvent).where(FraudEvent.transaction_id.in_([from_mumbai, from_london]))
        ).scalars()
    }
    first, second = events[from_mumbai], events[from_london]
    assert first.decision == "STEP_UP" and first.risk_score == 35
    assert [r["code"] for r in first.reasons] == ["IMPOSSIBLE_TRAVEL"]
    assert first.reasons[0]["source"] == "RULE"
    assert (first.features["geo_lat"], first.features["geo_lon"]) == MUMBAI
    for key in ("travel_distance_km", "travel_elapsed_minutes", "travel_speed_kmh"):
        assert isinstance(first.features[key], float)
    assert first.features["travel_prior_country"] == "GB"
    assert LONDON_IP not in str(first.features) and MUMBAI_IP not in str(first.features)

    assert second.features["travel_prior_event"] == "Payment"
    # no country file configured: the country comes from the city file's record (ADR-0034)
    assert first.features["ip_country"] == "IN" and second.features["ip_country"] == "GB"
    assert second.features["travel_prior_country"] == "IN"
    travel = [r for r in second.reasons if r["code"] == "IMPOSSIBLE_TRAVEL"]
    assert len(travel) == 1 and "Payment from IN" in travel[0]["message"]
    assert "this payment from GB" in travel[0]["message"]
    # with NEW_COUNTRY (+20) and the new network (+15) the same jump reaches REVIEW (ADR-0034)
    assert {"NEW_COUNTRY", "NEW_DEVICE_OR_NETWORK"} <= {r["code"] for r in second.reasons}
    assert second.risk_score == 70 and second.decision == "REVIEW"
    speed = second.features["travel_speed_kmh"]
    assert speed is None or (isinstance(speed, float) and not math.isnan(speed) and speed > 900)


def test_a_recorded_payment_is_stamped_by_the_clock_the_next_payment_reads(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    located_transfers: Callable[[str], None],
) -> None:
    """#371: ``fraud_events.created_at`` came from the database's ``now()`` (its transaction start,
    on the database host's clock) while the next payment's ``occurred_at`` is the app's clock.
    With the database clock ahead (Docker Desktop's VM drifts by hundreds of ms) a payment made a
    moment earlier was "in the future" and travel scoring skipped it as the prior location."""
    alice, bob = make_customer(funds=1_000_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    located_transfers(MUMBAI_IP)
    transaction_id = _transfer(client, alice, payee)
    result = FraudScoreResult(
        risk_score=0, decision=FraudDecision.ALLOW, reasons=[], features={},
        model_version="rules-v1", latency_ms=1.0,
    )  # fmt: skip

    with Session(migrated_engine) as session:
        session.execute(select(1))  # opens the transaction: the database's now() is fixed here
        before = utcnow()
        event = fraud_service.record_event(session, transaction_id, alice.customer_id, result)
        session.commit()
        after = utcnow()
        assert before <= event.created_at <= after


def test_bill_payment_fills_the_location_and_is_scored_for_travel(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
    located_transfers: Callable[[str], None],
) -> None:
    alice = make_customer(funds=500_000)
    _login(db, alice, LONDON_IP, utcnow() - timedelta(hours=2))
    located_transfers(MUMBAI_IP)
    response = client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={"from_account_id": str(alice.account_id), "currency": "INR",
              "biller_id": "00000000-0000-0000-0000-0000000000b1",
              "consumer_reference": "CA-11112222", "amount_minor": 1_000},
    )  # fmt: skip
    assert response.status_code == 202, response.text  # bills have no step-up: held for review
    assert "reasons" not in response.text and "risk" not in response.text
    event = db.execute(
        select(FraudEvent).where(
            FraudEvent.transaction_id == uuid.UUID(response.json()["transaction_id"])
        )
    ).scalar_one()
    assert (event.features["geo_lat"], event.features["geo_lon"]) == MUMBAI
    assert [r["code"] for r in event.reasons] == ["IMPOSSIBLE_TRAVEL"]


@pytest.mark.parametrize("city_path", [None, ""])
def test_with_no_city_database_the_payment_path_is_unchanged(
    app: FastAPI,
    client: TestClient,
    settings: Settings,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    db: Session,
    city_path: str | None,
) -> None:
    """Post-merge review of #365: with GEOIP_CITY_DB_PATH unset or empty, a transfer that WOULD be
    impossible travel (London sign-in 2 h earlier, request from Mumbai; STEP_UP 35 when the city
    file is configured, see the test above) is scored exactly as before the rule existed: no
    lookup, no reason, no score, POSTED. The real ``get_location_resolver`` is used (not
    overridden); ``model_copy`` skips validation, so ``""`` also exercises the resolver's own guard.
    """
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"geoip_city_db_path": city_path}
    )
    app.dependency_overrides[get_request_meta] = lambda: RequestMeta(ip=MUMBAI_IP)
    alice, bob = make_customer(funds=1_000_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    _login(db, alice, LONDON_IP, utcnow() - timedelta(hours=2))

    transaction_id = _transfer(client, alice, payee)

    event = db.execute(
        select(FraudEvent).where(FraudEvent.transaction_id == transaction_id)
    ).scalar_one()
    assert event.decision == "ALLOW" and event.risk_score == 0 and event.reasons == []
    assert event.features["location_unknown"] is True
    assert event.features["geo_lat"] is None and event.features["geo_lon"] is None
    assert event.features["ip_country"] is None  # no country file either: as before #365
    assert not any(key.startswith("travel_") for key in event.features)
