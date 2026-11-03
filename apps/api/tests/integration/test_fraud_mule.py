"""MULE_PASS_THROUGH (#144, Blueprint F6, ADR-0035) on real transfers and real PostgreSQL.

Money received from another customer and most of it sent straight on is flagged; the funding path
(DEPOSIT), the customer's own accounts, small or stale inflows and a partial forward are not. The
scorer's clock is injected, so "20 minutes later" and "3 hours later" need no sleeping.
"""

import statistics
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import get_session, utcnow
from pbip_api.modules.fraud.baseline import BaselineRulesScorer
from pbip_api.modules.fraud.dependencies import get_fraud_scorer
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import TransactionType
from pbip_ml.fraud.contract import FraudDecision, FraudScorer, TransactionContext
from pbip_test_support import Actor, age_payee
from pydantic import ValidationError
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

RECEIVED = 5_000_000  # INR 50,000.00


@pytest.fixture
def offset(app: FastAPI, settings: Settings) -> Iterator[list[timedelta]]:
    """The API's scorer reads ``utcnow() + offset[0]``: set it to score "later"."""
    shift = [timedelta(0)]

    def scorer(session: Session = Depends(get_session)) -> FraudScorer:
        return BaselineRulesScorer(session, settings=settings, clock=lambda: utcnow() + shift[0])

    app.dependency_overrides[get_fraud_scorer] = scorer
    yield shift
    app.dependency_overrides.pop(get_fraud_scorer, None)


@pytest.fixture
def mule(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> tuple[Actor, dict[str, Any], Actor, dict[str, Any]]:
    """Sender A (funded by deposit) with a payee for B; B with a payee for C. Payees are 48 h
    old, so the new-payee rules stay quiet and only the pass-through rule can fire."""
    a, b, c = make_customer(funds=50_000_000), make_customer(), make_customer()
    a_to_b, b_to_c = make_verified_payee(a, b), make_verified_payee(b, c)
    age_payee(migrated_engine, a_to_b["id"], 48)
    age_payee(migrated_engine, b_to_c["id"], 48)
    return a, a_to_b, b, b_to_c


def pay(
    client: TestClient,
    sender: Actor,
    amount: int,
    payee: dict[str, Any] | None = None,
    *,
    from_account: uuid.UUID | None = None,
    to_account: uuid.UUID | None = None,
) -> Any:
    destination = {"payee_id": payee["id"]} if payee else {"to_account_id": str(to_account)}
    response = client.post(
        "/api/v1/transfers",
        headers={**sender.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
        json={"from_account_id": str(from_account or sender.account_id), "amount_minor": amount,
              "reference": "mule test", **destination},
    )  # fmt: skip
    assert response.status_code in (201, 202), response.text
    return response.json()


def event(db: Session, body: Any) -> FraudEvent:
    db.expire_all()
    return db.execute(
        select(FraudEvent).where(FraudEvent.transaction_id == uuid.UUID(body["id"]))
    ).scalar_one()


def mule_reason(e: FraudEvent) -> dict[str, Any] | None:
    return next((r for r in e.reasons if r["code"] == "MULE_PASS_THROUGH"), None)


def backdate(engine: Engine, transaction_id: str, by: timedelta) -> None:
    """Move one transaction header's timestamps into the past (test-only, like ``post_spend_at``;
    balances and entries are untouched, production never rewrites a ledger timestamp)."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE transactions SET created_at = created_at - :by, "
                "posted_at = posted_at - :by WHERE id = :id"
            ),
            {"by": by, "id": transaction_id},
        )


def score_now(
    engine: Engine,
    settings: Settings,
    actor: Actor,
    amount: int,
    at: Callable[[], datetime] = utcnow,
) -> Any:
    with sessionmaker(bind=engine)() as session:
        return BaselineRulesScorer(session, settings=settings, clock=at).score(
            TransactionContext(
                transaction_id=uuid.uuid4(),
                customer_id=actor.customer_id,
                account_id=actor.account_id,
                amount_minor=amount,
                currency="INR",
                occurred_at=utcnow(),
            )
        )


def test_pass_through_is_flagged_with_its_evidence(
    client: TestClient, mule: tuple[Actor, dict[str, Any], Actor, dict[str, Any]],
    offset: list[timedelta], db: Session,
) -> None:  # fmt: skip
    a, a_to_b, b, b_to_c = mule
    inbound = pay(client, a, RECEIVED, a_to_b)
    assert inbound["status"] == "POSTED"
    offset[0] = timedelta(minutes=20)
    forward = pay(client, b, 4_500_000, b_to_c)
    e = event(db, forward)
    reason = mule_reason(e)
    assert reason is not None and reason["source"] == "RULE" and reason["contribution"] == 45
    assert reason["message"] == (
        "Received INR 50,000.00 in 1 transfer from other customers, the first 20 min ago; "
        "sending INR 45,000.00 (90%)"
    )
    assert e.risk_score == 45 and e.decision == FraudDecision.STEP_UP
    assert e.model_version == "baseline-rules-0.4"
    assert e.features["mule_pass_through"] is True
    assert e.features["mule_inbound_total_minor"] == RECEIVED
    assert e.features["mule_inbound_count"] == 1 and e.features["mule_window_minutes"] == 60
    assert e.features["mule_ratio"] == 0.9
    assert e.features["mule_inbound_transaction_ids"] == inbound["id"]
    # The customer sees a hold, never the rule, the score or the inbound ids.
    assert forward["status"] == "PENDING_REVIEW"  # B has no MFA: STEP_UP becomes REVIEW
    assert (
        "MULE" not in str(forward)
        and "risk" not in str(forward)
        and inbound["id"] not in str(forward)
    )


def test_pass_through_with_a_new_payee_reaches_review(
    client: TestClient,
    mule: tuple[Actor, dict[str, Any], Actor, dict[str, Any]],
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    offset: list[timedelta],
    db: Session,
) -> None:
    a, a_to_b, b, _ = mule
    fresh = make_verified_payee(b, make_customer())  # added seconds ago: NEW_PAYEE (+15)
    pay(client, a, 2_400_000, a_to_b)
    e = event(db, pay(client, b, 2_280_000, fresh))  # the demo lab's F6: 95% of 24,000
    assert {r["code"] for r in e.reasons} == {"NEW_PAYEE", "MULE_PASS_THROUGH"}
    assert e.risk_score == 60 and e.decision == FraudDecision.REVIEW


def test_forwarding_only_part_of_the_inflow_is_not_flagged(
    client: TestClient, mule: tuple[Actor, dict[str, Any], Actor, dict[str, Any]],
    offset: list[timedelta], db: Session,
) -> None:  # fmt: skip
    a, a_to_b, b, b_to_c = mule
    pay(client, a, RECEIVED, a_to_b)
    offset[0] = timedelta(minutes=20)
    e = event(db, pay(client, b, 1_000_000, b_to_c))
    assert mule_reason(e) is None and e.decision == FraudDecision.ALLOW
    assert e.features["mule_pass_through"] is False and e.features["mule_ratio"] == 0.2


def test_inbound_older_than_the_window_is_not_counted(
    client: TestClient, mule: tuple[Actor, dict[str, Any], Actor, dict[str, Any]],
    offset: list[timedelta], db: Session,
) -> None:  # fmt: skip
    a, a_to_b, b, b_to_c = mule
    pay(client, a, RECEIVED, a_to_b)
    offset[0] = timedelta(hours=3)
    e = event(db, pay(client, b, 4_500_000, b_to_c))
    assert mule_reason(e) is None and e.features["mule_inbound_count"] == 0


def test_funding_by_deposit_is_not_inbound(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    db: Session,
) -> None:
    funded, other = make_customer(funds=RECEIVED), make_customer()
    payee = make_verified_payee(funded, other)
    age_payee(migrated_engine, payee["id"], 48)
    e = event(db, pay(client, funded, RECEIVED, payee))  # all of a fresh deposit, at once
    assert mule_reason(e) is None and e.features["mule_inbound_count"] == 0
    assert e.features["mule_inbound_total_minor"] == 0 and e.features["mule_ratio"] is None


def test_money_from_the_customers_own_account_is_not_inbound(
    client: TestClient, mule: tuple[Actor, dict[str, Any], Actor, dict[str, Any]],
    migrated_engine: Engine, db: Session,
) -> None:  # fmt: skip
    _, _, b, b_to_c = mule
    opened = client.post("/api/v1/accounts", headers=b.headers, json={"account_type": "SAVINGS"})
    assert opened.status_code == 201, opened.text
    second = uuid.UUID(opened.json()["id"])
    with sessionmaker(bind=migrated_engine)() as session:
        ledger.deposit(session, second, RECEIVED)
        session.commit()
    moved = pay(client, b, RECEIVED, from_account=second, to_account=b.account_id)
    assert moved["status"] == "POSTED"
    e = event(db, pay(client, b, 4_500_000, b_to_c))
    assert mule_reason(e) is None and e.features["mule_inbound_count"] == 0


def test_ratio_and_minimum_boundaries_are_exact(
    client: TestClient, mule: tuple[Actor, dict[str, Any], Actor, dict[str, Any]],
    migrated_engine: Engine, settings: Settings,
) -> None:  # fmt: skip
    a, a_to_b, b, _ = mule
    pay(client, a, RECEIVED, a_to_b)
    at_ratio = 4_000_000  # exactly 80% of INR 50,000.00
    fired = score_now(migrated_engine, settings, b, at_ratio)
    assert "MULE_PASS_THROUGH" in {r.code for r in fired.reasons}
    assert "(80%)" in next(r.message for r in fired.reasons if r.code == "MULE_PASS_THROUGH")
    below = score_now(migrated_engine, settings, b, at_ratio - 1)
    assert "MULE_PASS_THROUGH" not in {r.code for r in below.reasons}
    at_minimum = settings.model_copy(update={"fraud_mule_min_inbound_minor": RECEIVED})
    assert score_now(migrated_engine, at_minimum, b, at_ratio).features["mule_pass_through"]
    above = settings.model_copy(update={"fraud_mule_min_inbound_minor": RECEIVED + 1})
    assert score_now(migrated_engine, above, b, at_ratio).features["mule_pass_through"] is False


def test_third_episode_in_30_days_is_a_repeat_pattern_with_a_higher_weight(
    client: TestClient, mule: tuple[Actor, dict[str, Any], Actor, dict[str, Any]],
    offset: list[timedelta], migrated_engine: Engine, db: Session,
) -> None:  # fmt: skip
    a, a_to_b, b, b_to_c = mule
    contributions = []
    for days_ago in (20, 10, 0):
        inbound = pay(client, a, RECEIVED, a_to_b)
        forward = pay(client, b, 4_500_000, b_to_c)
        reason = mule_reason(event(db, forward))
        assert reason is not None
        contributions.append(reason["contribution"])
        if days_ago:  # the episode is in the past: its money is outside the next window
            backdate(migrated_engine, inbound["id"], timedelta(days=days_ago))
            backdate(migrated_engine, forward["id"], timedelta(days=days_ago))
    assert contributions == [45, 45, 60]
    third = event(db, forward)
    reason = mule_reason(third)
    assert reason is not None and reason["message"].endswith(
        "; repeat pattern: 2 earlier pass-throughs in 30 days"
    )
    assert third.features["mule_earlier_episodes_30d"] == 2
    assert third.risk_score == 60 and third.decision == FraudDecision.REVIEW


def test_concurrent_forwards_see_the_same_inbound_total(
    app: FastAPI,
    mule: tuple[Actor, dict[str, Any], Actor, dict[str, Any]],
    migrated_engine: Engine,
    client: TestClient,
    db: Session,
) -> None:
    a, a_to_b, b, b_to_c = mule
    with sessionmaker(bind=migrated_engine)() as session:
        ledger.deposit(session, b.account_id, 10_000_000)  # funding: never inbound
        session.commit()
    inbound = pay(client, a, RECEIVED, a_to_b)

    def forward(_: int) -> Any:
        return pay(TestClient(app), b, 4_500_000, b_to_c)

    with ThreadPoolExecutor(max_workers=2) as pool:
        bodies = list(pool.map(forward, range(2)))
    events = [event(db, body) for body in bodies]
    assert len({body["id"] for body in bodies}) == 2
    for e in events:
        assert e.features["mule_inbound_total_minor"] == RECEIVED
        assert e.features["mule_inbound_transaction_ids"] == inbound["id"]
        assert mule_reason(e) is not None


def test_scoring_stays_fast_with_1000_inbound_transfers(
    mule: tuple[Actor, dict[str, Any], Actor, dict[str, Any]],
    migrated_engine: Engine,
    settings: Settings,
) -> None:
    a, _, b, _ = mule
    with sessionmaker(bind=migrated_engine)() as session:
        for _ in range(1_000):
            txn = ledger.create_transaction(
                session,
                type_=TransactionType.TRANSFER,
                amount=10_000,
                currency="INR",
                debit_account_id=a.account_id,
                credit_account_id=b.account_id,
            )
            ledger.post_transaction(session, txn, ledger.transfer_legs(txn))
        session.commit()
    fixed = utcnow() + timedelta(minutes=5)
    latencies = []
    for call in range(43):  # the first 3 warm the connection pool and the plan cache
        result = score_now(migrated_engine, settings, b, 9_000_000, at=lambda: fixed)
        if call >= 3:
            latencies.append(result.latency_ms)
    assert result.features["mule_inbound_count"] == 1_000
    assert result.features["mule_inbound_total_minor"] == 10_000_000
    assert str(result.features["mule_inbound_transaction_ids"]).count(",") == 19  # capped at 20
    assert "MULE_PASS_THROUGH" in {r.code for r in result.reasons}
    p95 = statistics.quantiles(latencies, n=20)[-1]
    assert p95 < 100, latencies  # AGENTS.md §8: p95 < 100 ms, DB look-ups included


@pytest.mark.parametrize(
    "overrides",
    [
        {"fraud_mule_min_ratio_percent": 0},
        {"fraud_mule_min_ratio_percent": 101},
        {"fraud_mule_window_minutes": 0},
        {"fraud_mule_min_inbound_minor": 0},
        {"fraud_mule_repeat_episodes": 0},
    ],
)
def test_thresholds_are_validated(settings: Settings, overrides: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**settings.model_dump(), **overrides})
