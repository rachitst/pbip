"""API <-> ML feature parity for F7/F8 (#274, #275, ADR-0049), on real PostgreSQL.

The hybrid scorer builds the model's row from the platform's records. These tests put a known
history in the ledger (back-dated through the ledger service), score a transfer, and compare the
row the model received with ``pbip_ml``'s OFFLINE pipeline (what training uses) run on the same
history read straight from the ledger. Zero tolerance: the model must be served the numbers it was
trained on. A stub engine captures the row, so no trained artifact is needed.
"""

import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.modules.fraud.hybrid import ACTIVITY_TYPES
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import Transaction, TransactionStatus, TransactionType
from pbip_ml.fraud.features import FEATURE_NAMES, offline_features
from pbip_ml.fraud.hybrid import HybridResult, decide
from pbip_ml.fraud.rules import RuleContext
from pbip_test_support import Actor, _redate_transaction, age_payee, post_deposit_at
from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

JUST_UNDER = 9_500_000  # INR 95,000.00: 95% of the INR 1,00,000.00 reporting threshold


class StubEngine:
    """Stands in for ``HybridScorer``: records the row it is given and scores it 10 (ALLOW)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def score(self, row: pd.DataFrame, context: RuleContext) -> HybridResult:
        self.rows.append(row.iloc[0].to_dict())
        return HybridResult(10, decide(10), [], "parity-stub", {})


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"fraud_scorer": "hybrid"})


@pytest.fixture
def stub(app: FastAPI) -> Iterator[StubEngine]:
    engine = StubEngine()
    app.state.fraud_engine = engine
    yield engine
    app.state.fraud_engine = None


@pytest.fixture
def known_payee(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> Callable[..., dict[str, Any]]:
    """A payee added 48 h ago (the new-payee rules stay quiet), paying ``target`` or a stranger."""

    def factory(owner: Actor, target: Actor | None = None) -> dict[str, Any]:
        payee = make_verified_payee(owner, target or make_customer())
        age_payee(migrated_engine, payee["id"], 48)
        return payee

    return factory


def pay(client: TestClient, sender: Actor, payee: dict[str, Any], amount: int) -> Any:
    response = client.post(
        "/api/v1/transfers",
        headers={**sender.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
        json={"from_account_id": str(sender.account_id), "amount_minor": amount,
              "payee_id": payee["id"], "reference": "parity"},
    )  # fmt: skip
    assert response.status_code in (201, 202), response.text
    return response.json()


def post_at(engine: Engine, account_id: uuid.UUID, type_: TransactionType, amount: int,
            when: datetime) -> None:  # fmt: skip
    """A debit of ``type_`` to the clearing account through the ledger service, dated ``when``."""
    with sessionmaker(bind=engine)() as session:
        txn = ledger.create_transaction(
            session, type_=type_, amount=amount, currency="INR", debit_account_id=account_id,
            credit_account_id=ledger.SYSTEM_CLEARING_ID, reference=type_.value,
        )  # fmt: skip
        ledger.post_transaction(session, txn, ledger.transfer_legs(txn))
        session.commit()
        txn_id = txn.id
    _redate_transaction(engine, txn_id, when)


def ledger_frame(db: Session, actor: Actor, scored_id: uuid.UUID) -> tuple[pd.DataFrame, int]:
    """The customer's whole ledger as the offline pipeline's input, and the scored row's index.

    Written independently of the scorer's queries: every transaction touching the account, the
    side it is on, its device from the fraud snapshot, and activity = POSTED and customer-induced.
    """
    db.expire_all()
    account = actor.account_id
    rows = db.execute(
        select(Transaction, FraudEvent.features["device_id"].astext)
        .outerjoin(FraudEvent, FraudEvent.transaction_id == Transaction.id)
        .where(or_(Transaction.debit_account_id == account, Transaction.credit_account_id == account))
        .order_by(Transaction.created_at)
    ).all()  # fmt: skip
    records = []
    for txn, device in rows:
        debit = txn.debit_account_id == account
        records.append(
            {
                "customer_id": actor.customer_id,
                "occurred_at": txn.created_at,
                "direction": "DEBIT" if debit else "CREDIT",
                "amount_minor": txn.amount,
                "payee_added_at": None,  # payee age is not a windowed feature
                "device_id": uuid.UUID(device) if device and debit else None,
                "failed_logins_24h": 0,
                "activity": txn.status == TransactionStatus.POSTED and txn.type in ACTIVITY_TYPES,
                "id": txn.id,
            }
        )
    frame = pd.DataFrame(records)
    return frame, int(frame.index[frame["id"] == scored_id][0])


WINDOWED = [n for n in FEATURE_NAMES if n not in ("payee_age_seconds", "failure_count_24h")]


def assert_parity(stub: StubEngine, db: Session, actor: Actor, body: Any) -> dict[str, Any]:
    served = stub.rows[-1]
    frame, index = ledger_frame(db, actor, uuid.UUID(body["id"]))
    offline = offline_features(frame).loc[index]
    assert {n: served[n] for n in WINDOWED} == {n: offline[n] for n in WINDOWED}
    return served


def test_structuring_inputs_match_the_offline_pipeline(
    client: TestClient, stub: StubEngine, make_customer: Callable[..., Actor],
    known_payee: Callable[..., dict[str, Any]], migrated_engine: Engine, db: Session,
) -> None:  # fmt: skip
    """#274: deposits just under the threshold in 72 h reach the model; older ones, a fee of the
    same size and an amount at the threshold do not."""
    alice, bob = make_customer(funds=1_000_000), make_customer(funds=20_000_000)
    now = utcnow()
    for hours in (10, 30, 60, 80):  # 80 h is outside the window
        post_deposit_at(
            migrated_engine, alice.account_id, JUST_UNDER, "Cash", now - timedelta(hours=hours)
        )
    post_at(
        migrated_engine, alice.account_id, TransactionType.FEE, JUST_UNDER, now - timedelta(hours=5)
    )
    post_deposit_at(migrated_engine, alice.account_id, 10_000_000, "At", now - timedelta(hours=4))
    assert pay(client, bob, known_payee(bob, alice), 9_900_000)["status"] == "POSTED"
    body = pay(client, alice, known_payee(alice), 500_000)
    served = assert_parity(stub, db, alice, body)
    assert served["near_threshold_72h"] == 4  # 10, 30, 60 h deposits and Bob's INR 99,000.00
    assert served["days_since_last_activity"] == 0


def test_dormant_account_inputs_match_the_offline_pipeline(
    client: TestClient, stub: StubEngine, make_customer: Callable[..., Actor],
    known_payee: Callable[..., dict[str, Any]], migrated_engine: Engine, db: Session,
) -> None:  # fmt: skip
    """#275: the last customer activity was 200 days ago; a fee 10 days ago does not wake the
    account, and the scorer looks past its 30-day history to find that activity."""
    alice = make_customer()
    now = utcnow()
    post_deposit_at(
        migrated_engine, alice.account_id, 20_000_000, "Old", now - timedelta(days=200, hours=1)
    )
    post_at(
        migrated_engine, alice.account_id, TransactionType.FEE, 50_000, now - timedelta(days=10)
    )
    body = pay(client, alice, known_payee(alice), 5_000_000)
    served = assert_parity(stub, db, alice, body)
    assert served["days_since_last_activity"] == 200
    assert served["near_threshold_72h"] == 0


def test_an_active_account_is_zero_days_and_its_fees_are_ignored(
    client: TestClient, stub: StubEngine, make_customer: Callable[..., Actor],
    known_payee: Callable[..., dict[str, Any]], migrated_engine: Engine, db: Session,
) -> None:  # fmt: skip
    alice = make_customer()
    now = utcnow()
    post_deposit_at(
        migrated_engine, alice.account_id, 5_000_000, "Pay", now - timedelta(days=3, hours=2)
    )
    post_at(
        migrated_engine, alice.account_id, TransactionType.FEE, 10_000, now - timedelta(hours=1)
    )
    payee = known_payee(alice)
    body = pay(client, alice, payee, 100_000)
    served = assert_parity(stub, db, alice, body)
    assert served["days_since_last_activity"] == 3  # the fee an hour ago is not activity
    body = pay(client, alice, payee, 100_000)
    assert assert_parity(stub, db, alice, body)["days_since_last_activity"] == 0
