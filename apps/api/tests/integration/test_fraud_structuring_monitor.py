"""#274 / ADR-0052: the F7 deposit-side structuring monitor on real PostgreSQL.

Credits are posted through ``ledger.deposit`` or the transfer API and then dated (test-only
history), and the job runs with an injected ``now``: nothing sleeps. The job opens alerts in the
existing analyst queue, audits each in the same transaction, never alerts a credit twice (re-run,
overlap, two workers at once) and never touches money.
"""

import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.fraud.hybrid import HybridFraudScorer
from pbip_api.modules.fraud.models import AlertStatus, FraudAlert, FraudEvent
from pbip_api.modules.fraud.structuring_monitor import (
    MONITOR_VERSION,
    REASON_CODE,
    run_structuring_monitor,
)
from pbip_api.modules.ledger.models import Transaction, TransactionStatus
from pbip_api.modules.notifications.models import OutboxEvent
from pbip_ml.fraud.contract import TransactionContext
from pbip_test_support import Actor, age_payee, post_deposit_at
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

# Hang guard of 300 s, as test_fraud_training (#362): on the shared 2-CPU laptop the first request
# and the one-time LightGBM/scikit-learn import in the hybrid-history test took over 60 s.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

JUST_UNDER = 9_950_000  # INR 99,500.00


@pytest.fixture
def now() -> datetime:
    return utcnow()


@pytest.fixture
def deposit_at(migrated_engine: Engine, now: datetime) -> Callable[..., uuid.UUID]:
    def post(account: uuid.UUID, hours_ago: float, amount: int = JUST_UNDER) -> uuid.UUID:
        return post_deposit_at(
            migrated_engine, account, amount, "cash deposit", now - timedelta(hours=hours_ago)
        )

    return post


def run(engine: Engine, now: datetime, settings: Settings) -> dict[str, int]:
    return run_structuring_monitor(engine, now=now, settings=settings)


def monitor_events(db: Session, account: uuid.UUID | None = None) -> list[FraudEvent]:
    db.expire_all()
    stmt = select(FraudEvent).where(FraudEvent.model_version == MONITOR_VERSION)
    if account is not None:
        stmt = stmt.where(FraudEvent.features["structuring_account_id"].astext == str(account))
    return list(db.execute(stmt.order_by(FraudEvent.created_at)).scalars())


def ids_of(event: FraudEvent) -> set[str]:
    return set(str(event.features["structuring_transaction_ids"]).split(","))


def pay(client: TestClient, sender: Actor, amount: int, *, payee: dict[str, Any] | None = None,
        from_account: uuid.UUID | None = None, to_account: uuid.UUID | None = None) -> Any:  # fmt: skip
    destination = {"payee_id": payee["id"]} if payee else {"to_account_id": str(to_account)}
    response = client.post(
        "/api/v1/transfers",
        headers={**sender.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
        json={"from_account_id": str(from_account or sender.account_id), "amount_minor": amount,
              "reference": "monitor test", **destination},
    )  # fmt: skip
    assert response.status_code in (201, 202), response.text
    return response.json()


def sent_after_a_deposit(
    client: TestClient, engine: Engine, make_verified_payee: Callable[..., dict[str, Any]],
    sender: Actor, receiver: Actor,
) -> uuid.UUID:  # fmt: skip
    """A deposit, then two in-band transfers from another customer: the pattern ends on the
    sender's transfer. (A third from one sender would be held by its own STRUCTURING rule.)"""
    assert receiver.account_id is not None
    post_deposit_at(engine, receiver.account_id, JUST_UNDER, "cash", utcnow() - timedelta(hours=1))
    payee = make_verified_payee(sender, receiver)
    age_payee(engine, payee["id"], 48)
    bodies = [pay(client, sender, JUST_UNDER, payee=payee) for _ in range(2)]
    assert [b["status"] for b in bodies] == ["POSTED", "POSTED"]
    return uuid.UUID(bodies[-1]["id"])


def test_three_deposits_just_under_the_threshold_open_one_audited_analyst_alert(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor],
    deposit_at: Callable[..., uuid.UUID], migrated_engine: Engine, db: Session,
    settings: Settings, now: datetime,
) -> None:  # fmt: skip
    b = make_customer()
    assert b.account_id is not None
    posted = [deposit_at(b.account_id, h) for h in (50, 30, 2)]
    before = client.get(f"/api/v1/accounts/{b.account_id}", headers=b.headers).json()

    assert run(migrated_engine, now, settings) == {"accounts": 1, "alerts": 1, "errors": 0}

    (event,) = monitor_events(db)
    assert event.transaction_id == posted[-1] and event.customer_id == b.customer_id
    assert (event.decision, event.risk_score) == ("REVIEW", 60)
    assert ids_of(event) == {str(t) for t in posted}
    assert event.features["structuring_count"] == 3
    assert event.features["structuring_total_minor"] == 3 * JUST_UNDER
    (reason,) = event.reasons
    assert reason["code"] == REASON_CODE and reason["source"] == "RULE"
    assert reason["message"] == (
        "3 credits just under INR 1,00,000.00 (at least 90% of it) within 48.0 h; "
        "together INR 2,98,500.00"
    )
    alert = db.execute(select(FraudAlert).where(FraudAlert.fraud_event_id == event.id)).scalar_one()
    assert alert.status == AlertStatus.OPEN and alert.severity.value == "MEDIUM"
    audit = db.execute(
        select(AuditLog).where(
            AuditLog.action == "FRAUD_STRUCTURING_ALERT_OPENED",
            AuditLog.resource_id == str(alert.id),
        )
    ).scalar_one()
    assert audit.actor_user_id is None
    assert audit.details == {
        "actor": "scheduler", "monitor": "structuring_deposits", "account_id": str(b.account_id),
        "credits": 3, "total_minor": 3 * JUST_UNDER,
    }  # fmt: skip

    # The analyst sees it in the existing queue; money and the customer's view are unchanged.
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    queue = client.get("/api/v1/fraud/alerts?status=OPEN", headers=analyst.headers).json()
    assert [(a["id"], a["transaction_status"]) for a in queue] == [(str(alert.id), "POSTED")]
    assert queue[0]["top_reason"] == reason["message"]
    after = client.get(f"/api/v1/accounts/{b.account_id}", headers=b.headers).json()
    assert after == before
    assert (
        "STRUCTURED"
        not in client.get(f"/api/v1/accounts/{b.account_id}/transactions", headers=b.headers).text
    )


def test_rerun_and_overlapping_windows_never_alert_the_same_credit_twice(
    make_customer: Callable[..., Actor], deposit_at: Callable[..., uuid.UUID],
    migrated_engine: Engine, db: Session, settings: Settings, now: datetime,
) -> None:  # fmt: skip
    b = make_customer()
    assert b.account_id is not None
    first = [deposit_at(b.account_id, h) for h in (100, 90, 80)]
    assert run(migrated_engine, now - timedelta(hours=70), settings)["alerts"] == 1
    assert run(migrated_engine, now - timedelta(hours=70), settings)["alerts"] == 0  # re-run
    deposit_at(b.account_id, 60)  # overlaps the first window, but one new credit is no pattern
    assert run(migrated_engine, now - timedelta(hours=59), settings)["alerts"] == 0
    second = [deposit_at(b.account_id, h) for h in (40, 20)]
    assert run(migrated_engine, now, settings)["alerts"] == 1
    assert run(migrated_engine, now, settings)["alerts"] == 0
    events = monitor_events(db, b.account_id)
    assert len(events) == 2
    assert ids_of(events[0]) == {str(t) for t in first}
    assert len(ids_of(events[1])) == 3 and {str(t) for t in second} <= ids_of(events[1])
    assert not ids_of(events[0]) & ids_of(events[1])


def test_two_workers_running_at_once_open_one_alert(
    make_customer: Callable[..., Actor], deposit_at: Callable[..., uuid.UUID],
    migrated_engine: Engine, db: Session, settings: Settings, now: datetime,
) -> None:  # fmt: skip
    b = make_customer()
    assert b.account_id is not None
    for h in (5, 4, 3):
        deposit_at(b.account_id, h)
    barrier, results = threading.Barrier(2), []

    def worker() -> None:
        barrier.wait()
        results.append(run(migrated_engine, now, settings))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(r["alerts"] for r in results) == [0, 1]
    assert all(r["errors"] == 0 for r in results)
    assert len(monitor_events(db, b.account_id)) == 1


@pytest.mark.parametrize(
    ("amount", "flagged"),
    [(9_000_000, True), (8_999_999, False), (9_999_999, True), (10_000_000, False)],
)
def test_band_edges_are_exact(
    make_customer: Callable[..., Actor], deposit_at: Callable[..., uuid.UUID],
    migrated_engine: Engine, settings: Settings, now: datetime, amount: int, flagged: bool,
) -> None:  # fmt: skip
    b = make_customer()
    assert b.account_id is not None
    for h in (3, 2, 1):
        deposit_at(b.account_id, h, amount)
    assert run(migrated_engine, now, settings)["alerts"] == int(flagged)


@pytest.mark.parametrize(("span_seconds", "flagged"), [(72 * 3600, True), (72 * 3600 + 1, False)])
def test_the_window_edge_is_inclusive(
    make_customer: Callable[..., Actor], deposit_at: Callable[..., uuid.UUID],
    migrated_engine: Engine, settings: Settings, now: datetime, span_seconds: int, flagged: bool,
) -> None:  # fmt: skip
    b = make_customer()
    assert b.account_id is not None
    first = 60 + span_seconds / 3600  # inside the 144 h lookback
    for h in (first, 80, 60):
        deposit_at(b.account_id, h)
    assert run(migrated_engine, now, settings)["alerts"] == int(flagged)


def test_two_credits_are_not_a_pattern_unless_the_setting_says_so(
    make_customer: Callable[..., Actor], deposit_at: Callable[..., uuid.UUID],
    migrated_engine: Engine, settings: Settings, now: datetime,
) -> None:  # fmt: skip
    b = make_customer()
    assert b.account_id is not None
    deposit_at(b.account_id, 3)
    deposit_at(b.account_id, 2)
    assert run(migrated_engine, now, settings)["alerts"] == 0
    two = settings.model_copy(update={"fraud_structuring_min_count": 2})
    assert run(migrated_engine, now, two)["alerts"] == 1


def test_credits_older_than_the_lookback_are_not_scanned(
    make_customer: Callable[..., Actor], deposit_at: Callable[..., uuid.UUID],
    migrated_engine: Engine, settings: Settings, now: datetime,
) -> None:  # fmt: skip
    b = make_customer()
    assert b.account_id is not None
    for h in (150, 149, 148):  # the lookback is two 72 h windows: 144 h
        deposit_at(b.account_id, h)
    assert run(migrated_engine, now, settings)["accounts"] == 0


def test_transfers_from_other_customers_count_own_moves_and_outgoing_do_not(
    client: TestClient, make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]], migrated_engine: Engine, db: Session,
    settings: Settings, now: datetime,
) -> None:  # fmt: skip
    b = make_customer()
    assert b.account_id is not None
    senders = [make_customer(funds=20_000_000) for _ in range(3)]
    for sender in senders:
        payee = make_verified_payee(sender, b)
        age_payee(migrated_engine, payee["id"], 48)
        assert pay(client, sender, JUST_UNDER, payee=payee)["status"] == "POSTED"
    assert run(migrated_engine, utcnow(), settings)["alerts"] == 1
    (event,) = monitor_events(db, b.account_id)
    assert event.customer_id == b.customer_id

    # Own-account moves are not counted; the receiver's moves out are not credits at all.
    c = make_customer(funds=40_000_000)
    assert c.account_id is not None
    opened = client.post("/api/v1/accounts", headers=c.headers, json={"account_type": "SAVINGS"})
    assert opened.status_code == 201, opened.text
    second = uuid.UUID(opened.json()["id"])
    for _ in range(3):
        pay(client, c, JUST_UNDER, to_account=second)
    assert run(migrated_engine, utcnow(), settings)["alerts"] == 0
    assert monitor_events(db, second) == [] and monitor_events(db, c.account_id) == []


def test_a_failure_leaves_no_partial_alert_and_the_next_run_opens_it(
    make_customer: Callable[..., Actor], deposit_at: Callable[..., uuid.UUID],
    migrated_engine: Engine, db: Session, settings: Settings, now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    b = make_customer()
    assert b.account_id is not None
    for h in (3, 2, 1):
        deposit_at(b.account_id, h)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("audit store unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(AuditService, "record", broken)
        assert run(migrated_engine, now, settings) == {"accounts": 1, "alerts": 0, "errors": 1}
    assert monitor_events(db) == []
    assert db.scalar(select(func.count()).select_from(FraudAlert)) == 0
    assert run(migrated_engine, now, settings)["alerts"] == 1


def test_resolving_a_monitor_alert_labels_it_and_touches_no_money_or_sender(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]], migrated_engine: Engine, db: Session,
    settings: Settings,
) -> None:  # fmt: skip
    b, sender = make_customer(), make_customer(funds=40_000_000)
    assert b.account_id is not None
    sent = sent_after_a_deposit(client, migrated_engine, make_verified_payee, sender, b)
    run(migrated_engine, utcnow(), settings)
    (event,) = monitor_events(db)
    assert event.transaction_id == sent  # the alert is on a transfer another customer sent
    alert = db.execute(select(FraudAlert).where(FraudAlert.fraud_event_id == event.id)).scalar_one()
    balance = client.get(f"/api/v1/accounts/{b.account_id}", headers=b.headers).json()
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    outbox_before = db.scalar(select(func.count()).select_from(OutboxEvent))
    decided = client.post(
        f"/api/v1/fraud/alerts/{alert.id}/decision",
        headers={**analyst.headers, "Idempotency-Key": f"decide-{alert.id}"},
        json={"action": "RELEASE", "note": "business receipts, documented"},
    )
    assert decided.status_code == 204, decided.text
    db.expire_all()
    assert db.get(FraudAlert, alert.id).status == AlertStatus.FALSE_POSITIVE  # type: ignore[union-attr]  # exists
    assert db.get(Transaction, event.transaction_id).status == TransactionStatus.POSTED  # type: ignore[union-attr]  # exists
    assert client.get(f"/api/v1/accounts/{b.account_id}", headers=b.headers).json() == balance
    assert db.scalar(select(func.count()).select_from(OutboxEvent)) == outbox_before  # no notice


def test_a_monitor_event_on_a_sent_transfer_does_not_duplicate_the_senders_model_history(
    client: TestClient, make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]], migrated_engine: Engine,
    settings: Settings,
) -> None:  # fmt: skip
    b, sender = make_customer(), make_customer(funds=40_000_000)
    assert sender.customer_id is not None and sender.account_id is not None
    sent_after_a_deposit(client, migrated_engine, make_verified_payee, sender, b)
    assert run(migrated_engine, utcnow(), settings)["alerts"] == 1
    with sessionmaker(bind=migrated_engine)() as session:
        scorer = HybridFraudScorer(session, None, 50, settings=settings)
        inputs = scorer._inputs(
            TransactionContext(
                transaction_id=uuid.uuid4(), customer_id=sender.customer_id,
                account_id=sender.account_id, amount_minor=10_000, currency="INR",
                occurred_at=utcnow(),
            ),
            new_device=False,
        )  # fmt: skip
    assert len(inputs.history) == 2  # one row per earlier debit, not one per fraud event


def test_payment_stats_leave_monitor_alerts_out(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor],
    deposit_at: Callable[..., uuid.UUID], migrated_engine: Engine, settings: Settings,
    now: datetime,
) -> None:  # fmt: skip
    b = make_customer()
    assert b.account_id is not None
    for h in (3, 2, 1):
        deposit_at(b.account_id, h)
    assert run(migrated_engine, now, settings)["alerts"] == 1
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    stats = client.get("/api/v1/fraud/stats", headers=analyst.headers).json()
    assert stats["scored"] == 0 and stats["latency_p95_ms"] is None


def test_run_reads_its_settings_through_the_band(settings: Settings) -> None:
    from pbip_api.modules.fraud.structuring_monitor import band_from

    band = band_from(settings)
    assert (band.threshold_minor, band.band_percent, band.min_count) == (10_000_000, 10, 3)
    assert band.window == timedelta(hours=72)
