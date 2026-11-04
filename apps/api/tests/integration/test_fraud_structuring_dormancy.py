"""STRUCTURING (F7, #274) and DORMANT_REACTIVATION (F8, #275), ADR-0046, on real PostgreSQL.

Structuring: several movements on the account within 72 h, each just under INR 1,00,000.00 (at
least 90% of it), counting the scored transfer; the threshold itself, amounts below the band,
moves between the customer's own accounts and anything outside the window are not counted.
Dormancy: a transfer of INR 25,000.00 or more from an account with no customer-induced activity
for 180 days before it woke up in the last 7 days; small amounts, active accounts and
bank-initiated postings (fees) do not count. Scorer clocks are injected; nothing sleeps.
"""

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.modules.fraud.baseline import STRUCTURING_SNAPSHOT_IDS, BaselineRulesScorer
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import TransactionType
from pbip_ml.fraud.contract import FraudDecision, FraudScoreResult, TransactionContext
from pbip_test_support import Actor, _redate_transaction, age_payee, post_deposit_at
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy import event as sql_event
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

JUST_UNDER = 9_950_000  # INR 99,500.00: 99.5% of the INR 1,00,000.00 threshold
LARGE = 5_000_000  # INR 50,000.00: at least the INR 25,000.00 dormant minimum
SMALL = 200_000  # INR 2,000.00


def pay(client: TestClient, sender: Actor, amount: int, payee: dict[str, Any] | None = None,
        *, from_account: uuid.UUID | None = None, to_account: uuid.UUID | None = None) -> Any:  # fmt: skip
    destination = {"payee_id": payee["id"]} if payee else {"to_account_id": str(to_account)}
    response = client.post(
        "/api/v1/transfers",
        headers={**sender.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
        json={"from_account_id": str(from_account or sender.account_id), "amount_minor": amount,
              "reference": "rule test", **destination},
    )  # fmt: skip
    assert response.status_code in (201, 202), response.text
    return response.json()


def event(db: Session, body: Any) -> FraudEvent:
    db.expire_all()
    return db.execute(
        select(FraudEvent).where(FraudEvent.transaction_id == uuid.UUID(body["id"]))
    ).scalar_one()


def reason(e: FraudEvent | FraudScoreResult, code: str) -> Any:
    reasons = [r if isinstance(r, dict) else r.model_dump() for r in e.reasons]
    return next((r for r in reasons if r["code"] == code), None)


def deposit(engine: Engine, account_id: uuid.UUID, amount: int) -> None:
    with sessionmaker(bind=engine)() as session:
        ledger.deposit(session, account_id, amount)
        session.commit()


def score_now(engine: Engine, settings: Settings, actor: Actor, amount: int,
              at: Callable[[], datetime] = utcnow) -> FraudScoreResult:  # fmt: skip
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


@pytest.fixture
def known_payee(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> Callable[[Actor], dict[str, Any]]:
    """A payee added 48 h ago, so the new-payee rules stay quiet."""

    def factory(owner: Actor) -> dict[str, Any]:
        payee = make_verified_payee(owner, make_customer())
        age_payee(migrated_engine, payee["id"], 48)
        return payee

    return factory


# --- STRUCTURING (F7) ----------------------------------------------------------------------


def test_deposits_just_under_the_threshold_flag_the_next_transfer(
    client: TestClient, make_customer: Callable[..., Actor],
    known_payee: Callable[[Actor], dict[str, Any]], migrated_engine: Engine, db: Session,
) -> None:  # fmt: skip
    b = make_customer()
    for _ in range(3):
        deposit(migrated_engine, b.account_id, JUST_UNDER)
    body = pay(client, b, 500_000, known_payee(b))
    e = event(db, body)
    found = reason(e, "STRUCTURING")
    assert found is not None and found["source"] == "RULE" and found["contribution"] == 35
    assert found["message"] == (
        "3 movements just under INR 1,00,000.00 (at least 90% of it) in 72 h: 3 received, "
        "0 sent; together INR 2,98,500.00"
    )
    assert e.risk_score == 35 and e.decision == FraudDecision.STEP_UP
    assert e.model_version == "baseline-rules-0.6"
    assert e.features["structuring"] is True
    assert e.features["structuring_received_count"] == 3
    assert e.features["structuring_sent_count"] == 0
    assert e.features["structuring_this_transfer"] is False
    assert e.features["structuring_total_minor"] == 3 * JUST_UNDER
    assert str(e.features["structuring_transaction_ids"]).count(",") == 2
    # The customer sees a hold (no MFA: STEP_UP becomes REVIEW), never the rule or the score.
    assert body["status"] == "PENDING_REVIEW"
    assert "STRUCTURING" not in str(body) and "risk" not in str(body)


def test_transfers_split_just_under_the_threshold_are_flagged_on_the_third(
    client: TestClient, make_customer: Callable[..., Actor],
    known_payee: Callable[[Actor], dict[str, Any]], db: Session,
) -> None:  # fmt: skip
    a = make_customer(funds=40_000_000)
    payee = known_payee(a)
    first, second = pay(client, a, 9_900_000, payee), pay(client, a, 9_900_000, payee)
    assert first["status"] == second["status"] == "POSTED"
    assert reason(event(db, second), "STRUCTURING") is None  # two are not a pattern
    third = event(db, pay(client, a, 9_900_000, payee))
    found = reason(third, "STRUCTURING")
    assert found is not None and found["message"] == (
        "3 movements just under INR 1,00,000.00 (at least 90% of it) in 72 h: 0 received, "
        "3 sent including this transfer; together INR 2,97,000.00"
    )
    assert third.features["structuring_sent_count"] == 2
    assert third.features["structuring_this_transfer"] is True
    assert third.risk_score == 35 and third.decision == FraudDecision.STEP_UP


def test_amounts_outside_the_band_do_not_count_and_the_boundaries_are_exact(
    make_customer: Callable[..., Actor], migrated_engine: Engine, settings: Settings
) -> None:
    b = make_customer()
    for amount in (9_000_000, 9_999_999, 8_999_999, 10_000_000):  # in, in, below, the threshold
        deposit(migrated_engine, b.account_id, amount)
    quiet = score_now(migrated_engine, settings, b, 500_000)
    assert reason(quiet, "STRUCTURING") is None
    assert quiet.features["structuring_received_count"] == 2
    assert quiet.features["structuring"] is False
    under_band = score_now(migrated_engine, settings, b, 8_999_999)  # 89.99999%: not counted
    assert reason(under_band, "STRUCTURING") is None
    at_threshold = score_now(migrated_engine, settings, b, 10_000_000)  # LARGE_AMOUNT's job
    assert reason(at_threshold, "STRUCTURING") is None
    assert {r.code for r in at_threshold.reasons} == {"LARGE_AMOUNT"}
    at_floor = score_now(migrated_engine, settings, b, 9_000_000)  # exactly 90%: counted
    assert reason(at_floor, "STRUCTURING") is not None


def test_movements_older_than_the_window_are_not_counted(
    make_customer: Callable[..., Actor], migrated_engine: Engine, settings: Settings
) -> None:
    b = make_customer()
    for _ in range(3):
        deposit(migrated_engine, b.account_id, JUST_UNDER)
    inside = utcnow() + timedelta(hours=71)
    assert reason(score_now(migrated_engine, settings, b, 500_000, lambda: inside), "STRUCTURING")
    later = utcnow() + timedelta(hours=73)
    stale = score_now(migrated_engine, settings, b, 500_000, lambda: later)
    assert reason(stale, "STRUCTURING") is None
    assert stale.features["structuring_received_count"] == 0
    assert stale.features["structuring_transaction_ids"] == ""


def test_moves_between_the_customers_own_accounts_are_not_counted(
    client: TestClient, make_customer: Callable[..., Actor],
    known_payee: Callable[[Actor], dict[str, Any]], migrated_engine: Engine, db: Session,
) -> None:  # fmt: skip
    b = make_customer()
    opened = client.post("/api/v1/accounts", headers=b.headers, json={"account_type": "SAVINGS"})
    assert opened.status_code == 201, opened.text
    second = uuid.UUID(opened.json()["id"])
    deposit(migrated_engine, second, 3 * JUST_UNDER)
    for _ in range(3):
        moved = pay(client, b, JUST_UNDER, from_account=second, to_account=b.account_id)
        assert moved["status"] == "POSTED"
    e = event(db, pay(client, b, 500_000, known_payee(b)))
    assert reason(e, "STRUCTURING") is None
    assert e.features["structuring_received_count"] == 0


def _receive_near_threshold(engine: Engine, sender: Actor, receiver: Actor, count: int) -> None:
    with sessionmaker(bind=engine)() as session:
        for _ in range(count):
            txn = ledger.create_transaction(
                session,
                type_=TransactionType.TRANSFER,
                amount=JUST_UNDER,
                currency="INR",
                debit_account_id=sender.account_id,
                credit_account_id=receiver.account_id,
            )
            ledger.post_transaction(session, txn, ledger.transfer_legs(txn))
        session.commit()


Sent = list[tuple[str, Any, int]]  # (statement, parameters, rows returned)


def _scoring_cost(engine: Engine, settings: Settings, actor: Actor,
                  now: datetime) -> tuple[FraudScoreResult, Sent]:  # fmt: skip
    """Score once; record every SQL statement sent, its parameters and the rows it returned."""
    sent: Sent = []

    def record(*args: Any) -> None:  # (conn, cursor, statement, parameters, context, many)
        sent.append((str(args[2]), args[3], int(args[1].rowcount)))

    sql_event.listen(engine, "after_cursor_execute", record)
    try:
        result = score_now(engine, settings, actor, 500_000, at=lambda: now)
    finally:
        sql_event.remove(engine, "after_cursor_execute", record)
    return result, sent


def _plan_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [plan, *(node for child in plan.get("Plans", []) for node in _plan_nodes(child))]


def _transaction_scans(engine: Engine, statement: str, parameters: Any) -> set[str]:
    """How PostgreSQL reads ``transactions`` for this statement on fresh statistics: the name of
    each ``ix_transactions_*`` index it scans, and ``Seq Scan`` if it reads the whole table."""
    with engine.connect() as connection:
        connection.exec_driver_sql("ANALYZE transactions")  # what autovacuum would leave
        raw = connection.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {statement}", parameters)
        plan = raw.scalar_one()
        connection.rollback()
    root = (json.loads(plan) if isinstance(plan, str) else plan)[0]["Plan"]
    scans: set[str] = set()
    for node in _plan_nodes(root):
        index = str(node.get("Index Name", ""))
        if index.startswith("ix_transactions_"):
            scans.add(index)
        elif node.get("Relation Name") == "transactions" and node["Node Type"] == "Seq Scan":
            scans.add("Seq Scan")
    return scans


# Hang guard, not a speed bar (as #362): posting 1,000 ledger transfers takes 20-40 s on a busy
# 2-CPU laptop, near pytest-timeout's 60 s default.
@pytest.mark.timeout(300)
def test_scoring_cost_does_not_grow_with_1000_near_threshold_inbound_transfers(
    make_customer: Callable[..., Actor], migrated_engine: Engine, settings: Settings
) -> None:
    """p95 < 100 ms (AGENTS.md section 8) rests on bounded work, asserted here as cost, not time.

    STRUCTURING counts and sums in PostgreSQL and fetches at most STRUCTURING_SNAPSHOT_IDS ids, so
    1,000 near-threshold credits cost the same SQL statements as 3, none returns more than 20 rows,
    and both look-ups can be served by the account/created_at indexes (a regression to fetching
    every row, an N+1 or an unindexable predicate fails here). A wall-clock p95 failed under load;
    the real latency is measured by scripts/benchmark_structuring_scoring.py (#317, #379, #412).
    """
    sender = make_customer(funds=1_003 * JUST_UNDER)
    few, many = make_customer(), make_customer()
    _receive_near_threshold(migrated_engine, sender, few, 3)
    _receive_near_threshold(migrated_engine, sender, many, 1_000)
    fixed = utcnow() + timedelta(minutes=5)

    few_result, few_sent = _scoring_cost(migrated_engine, settings, few, fixed)
    result, sent = _scoring_cost(migrated_engine, settings, many, fixed)

    assert few_result.features["structuring_received_count"] == 3
    assert reason(few_result, "STRUCTURING") is not None
    assert result.features["structuring_received_count"] == 1_000
    assert result.features["structuring_total_minor"] == 1_000 * JUST_UNDER
    assert str(result.features["structuring_transaction_ids"]).count(",") == 19  # capped at 20
    assert reason(result, "STRUCTURING") is not None
    assert [s for s, _, _ in sent] == [s for s, _, _ in few_sent], "statements grow with N"
    assert max(rows for _, _, rows in sent) <= STRUCTURING_SNAPSHOT_IDS
    # The plan is read for the 3-credit account: on a test-sized table the 1,000-credit account
    # holds most rows, where a sequential scan is the planner's right choice; on a real ledger
    # every account is a small share of the table, as this one is here.
    structuring = [(s, p) for s, p, _ in few_sent if "transactions.amount * " in s]
    assert len(structuring) == 2  # the count/sum and the capped id list
    for statement, parameters in structuring:
        assert _transaction_scans(migrated_engine, statement, parameters) == {
            "ix_transactions_credit_account_id_created_at",
            "ix_transactions_debit_account_id_created_at",
        }


# --- DORMANT_REACTIVATION (F8) -------------------------------------------------------------


@pytest.fixture
def dormant(make_customer: Callable[..., Actor], migrated_engine: Engine) -> Actor:
    """An account whose only activity, a deposit, was 200 days ago."""
    b = make_customer()
    post_deposit_at(
        migrated_engine, b.account_id, 10_000_000, "Old", utcnow() - timedelta(days=200)
    )
    return b


def test_dormant_account_moving_a_large_amount_to_a_new_payee_is_flagged(
    client: TestClient, dormant: Actor, make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]], db: Session,
) -> None:  # fmt: skip
    payee = make_verified_payee(dormant, make_customer())  # added seconds ago
    body = pay(client, dormant, LARGE, payee)
    e = event(db, body)
    found = reason(e, "DORMANT_REACTIVATION")
    assert found is not None and found["source"] == "RULE" and found["contribution"] == 35
    assert found["message"] == (
        "No customer activity on this account for 200 days until this transfer; "
        "sending INR 50,000.00"
    )
    assert {r["code"] for r in e.reasons} == {"NEW_PAYEE_LARGE_AMOUNT", "DORMANT_REACTIVATION"}
    assert e.risk_score == 80 and e.decision == FraudDecision.REVIEW
    assert e.features["dormant_reactivation"] is True and e.features["dormant_gap_days"] == 200
    assert e.features["dormancy_days"] == 180 and e.features["dormant_woke_at"] is None
    assert body["status"] == "PENDING_REVIEW"
    assert "DORMANT" not in str(body) and "risk" not in str(body)


def test_a_small_reactivation_is_not_flagged(
    client: TestClient, dormant: Actor, known_payee: Callable[[Actor], dict[str, Any]],
    db: Session,
) -> None:  # fmt: skip
    body = pay(client, dormant, SMALL, known_payee(dormant))
    e = event(db, body)
    assert reason(e, "DORMANT_REACTIVATION") is None and e.decision == FraudDecision.ALLOW
    assert e.features["dormant_reactivation"] is False and e.features["dormant_gap_days"] == 200
    assert body["status"] == "POSTED"


def test_a_small_first_transfer_does_not_hide_a_large_second(
    client: TestClient, dormant: Actor, known_payee: Callable[[Actor], dict[str, Any]],
    db: Session,
) -> None:  # fmt: skip
    payee = known_payee(dormant)
    assert pay(client, dormant, SMALL, payee)["status"] == "POSTED"
    e = event(db, pay(client, dormant, LARGE, payee))
    found = reason(e, "DORMANT_REACTIVATION")
    assert found is not None and found["message"] == (
        "No customer activity on this account for 200 days until it became active again 0 days "
        "ago; sending INR 50,000.00"
    )
    assert e.features["dormant_woke_at"] is not None


def test_an_active_account_is_not_dormant(
    client: TestClient, dormant: Actor, known_payee: Callable[[Actor], dict[str, Any]],
    migrated_engine: Engine, db: Session,
) -> None:  # fmt: skip
    post_deposit_at(migrated_engine, dormant.account_id, 100_000, "Recent",
                    utcnow() - timedelta(days=30))  # fmt: skip
    e = event(db, pay(client, dormant, LARGE, known_payee(dormant)))
    assert reason(e, "DORMANT_REACTIVATION") is None and e.features["dormant_gap_days"] == 30


def test_a_bank_posting_does_not_wake_a_dormant_account(
    dormant: Actor, migrated_engine: Engine, settings: Settings
) -> None:
    """A system posting (here interest credited by the bank; the FEE type no longer exists,
    #431) is not customer activity, so the account stays dormant."""
    with sessionmaker(bind=migrated_engine)() as session:
        interest = ledger.create_transaction(
            session,
            type_=TransactionType.FD_INTEREST,
            amount=10_000,
            currency="INR",
            debit_account_id=ledger.SYSTEM_CLEARING_ID,
            credit_account_id=dormant.account_id,
        )
        ledger.post_transaction(session, interest, ledger.transfer_legs(interest))
        session.commit()
        interest_id = interest.id
    _redate_transaction(migrated_engine, interest_id, utcnow() - timedelta(days=30))
    result = score_now(migrated_engine, settings, dormant, LARGE)
    assert reason(result, "DORMANT_REACTIVATION") is not None
    assert result.features["dormant_gap_days"] == 200


def test_an_account_never_used_since_it_was_opened_is_dormant(
    make_customer: Callable[..., Actor], migrated_engine: Engine, settings: Settings
) -> None:
    b = make_customer()
    later = utcnow() + timedelta(days=181)
    result = score_now(migrated_engine, settings, b, LARGE, at=lambda: later)
    assert reason(result, "DORMANT_REACTIVATION") is not None
    assert result.features["dormant_gap_days"] == 181
    fresh = score_now(migrated_engine, settings, b, LARGE)
    assert (
        reason(fresh, "DORMANT_REACTIVATION") is None and fresh.features["dormant_gap_days"] is None
    )


def test_the_minimum_amount_and_dormancy_boundaries_are_exact(
    dormant: Actor, migrated_engine: Engine, settings: Settings
) -> None:
    at_minimum = score_now(migrated_engine, settings, dormant, 2_500_000)
    assert reason(at_minimum, "DORMANT_REACTIVATION") is not None
    assert (
        reason(score_now(migrated_engine, settings, dormant, 2_499_999), "DORMANT_REACTIVATION")
        is None
    )
    at_gap = settings.model_copy(update={"fraud_dormancy_days": 200})
    assert reason(score_now(migrated_engine, at_gap, dormant, LARGE), "DORMANT_REACTIVATION")
    above_gap = settings.model_copy(update={"fraud_dormancy_days": 201})
    assert (
        reason(score_now(migrated_engine, above_gap, dormant, LARGE), "DORMANT_REACTIVATION")
        is None
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"fraud_structuring_threshold_minor": 19_999},
        {"fraud_structuring_band_percent": 0},
        {"fraud_structuring_band_percent": 51},
        {"fraud_structuring_window_hours": 0},
        {"fraud_structuring_min_count": 1},
        {"fraud_dormancy_days": 29},
        {"fraud_dormant_reactivation_days": 0},
        {"fraud_dormant_min_amount_minor": 0},
    ],
)
def test_thresholds_are_validated(settings: Settings, overrides: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**settings.model_dump(), **overrides})
