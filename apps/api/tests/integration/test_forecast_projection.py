"""TASK-043: end-of-month projection = model forecast + known obligations, and the low-balance
alert, against PostgreSQL.

Ledger entries are written at the real time while the request clock is moved forward, so the
account has a flat balance history and the TASK-042 model (seasonal naive on 20 flat days)
predicts that balance exactly. Every difference in the projection is then an obligation.
"""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.analytics import repository
from pbip_api.modules.analytics.projection import LOW_BALANCE_BUFFER_MINOR, month_end
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.loans import service as loans_service
from pbip_api.modules.loans.catalogue import PRODUCTS, LoanType
from pbip_api.modules.loans.models import LoanInstalment
from pbip_api.modules.notifications.models import Notification
from pbip_api.modules.notifications.service import (
    PROJECTED_LOW_BALANCE,
    notify_projected_low_balance,
)
from pbip_api.modules.payments import scheduled_service
from pbip_api.modules.payments.models import (
    ScheduledTransfer,
    ScheduledTransferStatus,
    ScheduleFrequency,
    SchedulePauseReason,
)
from pbip_api.modules.payments.scheduled_service import (
    ScheduledExecutionOutcome,
    execute_scheduled_instruction,
)
from pbip_test_support import Actor, FakeClock, age_payee
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

DAY = 24 * 3600
PERSONAL = next(p for p in PRODUCTS if p.loan_type == LoanType.PERSONAL)
INCOME = 1_00_000_00
LOAN = 1_00_000_00


@pytest.fixture(autouse=True)
def _worker_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """The scheduled engine reads ``get_settings()``; tests use the generated settings."""
    monkeypatch.setattr(scheduled_service, "get_settings", lambda: settings)


def _today(clock: FakeClock) -> date:
    return datetime.fromtimestamp(clock(), tz=UTC).date()


def _forecast(client: TestClient, actor: Actor, horizon: int = 30) -> Any:
    resp = client.get(
        f"/api/v1/analytics/forecast?account_id={actor.account_id}&horizon_days={horizon}",
        headers=actor.headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _instruction(
    session: Session,
    owner: Actor,
    payee_id: str,
    *,
    amount: int,
    frequency: ScheduleFrequency,
    next_run: date,
    status: ScheduledTransferStatus = ScheduledTransferStatus.ACTIVE,
    end_date: date | None = None,
) -> ScheduledTransfer:
    instruction = ScheduledTransfer(
        customer_id=owner.customer_id,
        user_id=owner.user_id,
        from_account_id=owner.account_id,
        payee_id=uuid.UUID(payee_id),
        amount=amount,
        currency="INR",
        reference="Rent",
        frequency=frequency,
        status=status,
        pause_reason=SchedulePauseReason.CUSTOMER
        if status == ScheduledTransferStatus.PAUSED
        else None,
        start_date=next_run,
        next_run_date=next_run,
        end_date=end_date,
    )
    session.add(instruction)
    session.commit()
    return instruction


@pytest.fixture
def income_for_the_loan(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests need a loan's EMIs, not an income history. Recurring income needs credits in
    two calendar months (ADR-0041), which would break the flat 20-day balance history the
    projection is checked against, so eligibility is given INCOME a month and no repayments."""
    monkeypatch.setattr(loans_service, "_income_and_debt", lambda *_: (INCOME, 0))


def _apply_for_loan(client: TestClient, actor: Actor) -> uuid.UUID:
    resp = client.post(
        "/api/v1/loans/apply",
        headers={**actor.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "product_id": str(PERSONAL.id),
            "amount_minor": LOAN,
            "tenure_months": 12,
            "account_id": str(actor.account_id),
        },
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def _instalments(session: Session, loan_id: uuid.UUID) -> list[LoanInstalment]:
    return list(
        session.execute(
            select(LoanInstalment).where(LoanInstalment.loan_id == loan_id).order_by("seq")
        ).scalars()
    )


def _assert_points_subtract_obligations(body: Any, balance: int) -> None:
    due: dict[str, int] = {}
    for obligation in body["obligations"]:
        due[obligation["date"]] = due.get(obligation["date"], 0) + obligation["amount_minor"]
    paid = 0
    for point in body["points"]:
        paid += due.get(point["date"], 0)
        assert point["obligations_minor"] == due.get(point["date"], 0), point
        assert point["predicted_minor"] == balance - paid, point
        assert point["lower_95_minor"] <= point["lower_80_minor"] <= point["predicted_minor"]
        assert point["predicted_minor"] <= point["upper_80_minor"] <= point["upper_95_minor"]


def test_projection_subtracts_each_scheduled_transfer_and_emi_on_its_date(
    client: TestClient,
    db: Session,
    clock: FakeClock,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    income_for_the_loan: None,
) -> None:
    alice, bob = make_customer(funds=INCOME), make_customer(funds=INCOME)
    loan_id = _apply_for_loan(client, alice)  # first EMI one month after today (real date)
    payee = make_verified_payee(alice, bob)
    bobs_payee = make_verified_payee(bob, alice)
    age_payee(migrated_engine, payee["id"], 48)
    clock.advance(20 * DAY)  # twenty flat days of history behind the request's "today"
    today = _today(clock)
    until = max(today + timedelta(days=29), month_end(today))

    monthly = _instruction(
        db, alice, payee["id"], amount=7_500_00, frequency=ScheduleFrequency.MONTHLY,
        next_run=today + timedelta(days=3),
    )  # fmt: skip
    weekly = _instruction(
        db, alice, payee["id"], amount=1_250_00, frequency=ScheduleFrequency.WEEKLY,
        next_run=today, end_date=today + timedelta(days=14),
    )  # fmt: skip
    # Not obligations: a paused order, a missed occurrence (the engine skips it) and Bob's order.
    _instruction(
        db, alice, payee["id"], amount=9_999_00, frequency=ScheduleFrequency.MONTHLY,
        next_run=today + timedelta(days=1), status=ScheduledTransferStatus.PAUSED,
    )  # fmt: skip
    _instruction(
        db, alice, payee["id"], amount=3_333_00, frequency=ScheduleFrequency.MONTHLY,
        next_run=today - timedelta(days=2), end_date=today - timedelta(days=1),
    )  # fmt: skip
    _instruction(
        db, bob, bobs_payee["id"], amount=4_444_00, frequency=ScheduleFrequency.DAILY,
        next_run=today,
    )  # fmt: skip

    body = _forecast(client, alice)

    emis = [i for i in _instalments(db, loan_id) if i.due_date <= until]
    assert emis, "the first EMI falls inside the projection"
    expected = (
        [
            (d, 7_500_00, "SCHEDULED_TRANSFER", str(monthly.id))
            for d in _monthly_dates(today + timedelta(days=3), until)
        ]
        + [
            (today + timedelta(days=d), 1_250_00, "SCHEDULED_TRANSFER", str(weekly.id))
            for d in (0, 7, 14)
        ]
        + [(i.due_date, i.amount, "LOAN_EMI", None) for i in emis]
    )
    got = [
        (
            date.fromisoformat(o["date"]),
            o["amount_minor"],
            o["kind"],
            o["scheduled_transfer_id"] if o["kind"] == "SCHEDULED_TRANSFER" else None,
        )
        for o in body["obligations"]
    ]
    assert sorted(got) == sorted(expected)
    assert [o[0] for o in got] == sorted(o[0] for o in got)  # in date order
    assert all(o["loan_id"] == str(loan_id) for o in body["obligations"] if o["kind"] == "LOAN_EMI")

    balance = INCOME + LOAN
    _assert_points_subtract_obligations(body, balance)
    eom = body["end_of_month"]
    in_month = sum(amount for d, amount, _, _ in got if d <= month_end(today))
    assert eom["date"] == month_end(today).isoformat()
    assert eom["obligations_minor"] == in_month
    assert eom["predicted_minor"] == balance - in_month
    assert eom["alert_threshold_minor"] == LOW_BALANCE_BUFFER_MINOR
    assert eom["low_balance"] is False
    assert len(body["history"]) == 20
    assert {h["balance_minor"] for h in body["history"]} == {balance}
    assert body["history"][-1]["date"] == (today - timedelta(days=1)).isoformat()


def _monthly_dates(first: date, until: date) -> list[date]:
    dates, current = [], first
    while current <= until:
        dates.append(current)
        month = current.month % 12 + 1
        year = current.year + (current.month == 12)
        day = min(first.day, month_end(date(year, month, 1)).day)
        current = date(year, month, day)
    return dates


def test_an_overdue_emi_is_projected_today_and_the_month_end_is_always_covered(
    client: TestClient,
    db: Session,
    clock: FakeClock,
    make_customer: Callable[..., Actor],
    income_for_the_loan: None,
) -> None:
    alice = make_customer(funds=INCOME)
    loan_id = _apply_for_loan(client, alice)
    first, second = _instalments(db, loan_id)[:2]
    clock.advance((first.due_date - utcnow().date()).days * DAY + 3 * DAY)  # 3 days in arrears
    today = _today(clock)

    body = _forecast(client, alice, horizon=7)  # shorter than the month: the month end is still in

    emis = [(o["date"], o["amount_minor"]) for o in body["obligations"]]
    assert emis[0] == (today.isoformat(), first.amount)  # the engine retries arrears daily
    until = max(today + timedelta(days=6), month_end(today))  # the projection's last day
    assert ((second.due_date.isoformat(), second.amount) in emis) is (second.due_date <= until)
    assert len(emis) == 1 + (second.due_date <= until)
    assert len(body["points"]) == 7
    assert body["points"][0]["predicted_minor"] == INCOME + LOAN - first.amount
    assert body["end_of_month"]["date"] == month_end(today).isoformat()


def test_scheduled_debits_in_the_history_are_found_and_reversals_ignored(
    client: TestClient,
    db: Session,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    """The add-back that stops last month's EMI being forecast again reads exactly these."""
    alice, bob = make_customer(funds=INCOME), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    today = utcnow().date()
    kept = _instruction(
        db, alice, payee["id"], amount=2_000_00, frequency=ScheduleFrequency.MONTHLY, next_run=today
    )
    undone = _instruction(
        db, alice, payee["id"], amount=900_00, frequency=ScheduleFrequency.MONTHLY, next_run=today
    )
    for instruction in (kept, undone):
        with Session(migrated_engine, expire_on_commit=False) as session:
            outcome = execute_scheduled_instruction(session, instruction.id, today_utc=today)
            assert outcome == ScheduledExecutionOutcome.POSTED
    db.refresh(undone)
    assert undone.last_transaction_id is not None
    ledger.reverse_transaction(db, undone.last_transaction_id, None, "customer dispute")
    db.commit()
    web = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={"from_account_id": str(alice.account_id), "amount_minor": 500_00,
              "payee_id": payee["id"]},
    )  # fmt: skip
    assert web.status_code == 201, web.text

    tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    assert alice.account_id is not None
    found = repository.scheduled_debits_by_day(db, alice.account_id, tomorrow, 180)
    assert found == {today: 2_000_00}


def _low_balance_alerts(session: Session, user_id: uuid.UUID) -> list[Notification]:
    return list(
        session.execute(
            select(Notification).where(
                Notification.user_id == user_id, Notification.type == PROJECTED_LOW_BALANCE
            )
        ).scalars()
    )


@pytest.mark.parametrize(
    ("funds", "overdraft", "alerted"),
    [
        (5_000_00, 0, False),  # month ends at INR 1,000.01 above the INR 1,000 threshold
        (4_999_99, 0, True),  # exactly INR 1,000.00: at the threshold
        (4_000_00, 0, True),  # INR 1 left: approaching zero
        (4_000_00, 2_000_00, False),  # the overdraft moves the floor to -INR 2,000
        (2_000_00, 2_000_00, True),  # INR -1,999 projected: approaching the overdraft limit
    ],
)
def test_low_month_end_balance_alerts_once_at_the_documented_threshold(
    client: TestClient,
    db: Session,
    clock: FakeClock,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    funds: int,
    overdraft: int,
    alerted: bool,
) -> None:
    alice, bob = make_customer(funds=funds), make_customer()
    payee = make_verified_payee(alice, bob)
    account = db.get(Account, alice.account_id)
    assert account is not None
    account.overdraft_limit = overdraft
    db.commit()
    clock.advance(20 * DAY)
    rent = 3_999_99
    _instruction(
        db, alice, payee["id"], amount=rent, frequency=ScheduleFrequency.MONTHLY,
        next_run=_today(clock),
    )  # fmt: skip

    body = _forecast(client, alice)
    eom = body["end_of_month"]
    assert eom["predicted_minor"] == funds - rent
    assert eom["alert_threshold_minor"] == LOW_BALANCE_BUFFER_MINOR - overdraft
    assert eom["low_balance"] is alerted

    _forecast(client, alice)  # the customer opens Insights again the same month
    alerts = _low_balance_alerts(db, alice.user_id)
    assert len(alerts) == (1 if alerted else 0)
    if alerted:
        (alert,) = alerts
        assert alert.account_id == alice.account_id
        assert "Predicted, not exact" in alert.body
        assert alice.account_number is not None and alice.account_number not in alert.body
        listed = client.get("/api/v1/notifications", headers=alice.headers).json()
        assert [n["type"] for n in listed["items"]].count(PROJECTED_LOW_BALANCE) == 1


def _send(session: Session, user_id: uuid.UUID, account_id: uuid.UUID, now: datetime) -> bool:
    return notify_projected_low_balance(
        session,
        user_id=user_id,
        account_id=account_id,
        account_masked="1234",
        month_end=month_end(now.date()),
        predicted_minor=10_00,
        currency="INR",
        now=now,
    )


NOVEMBER = datetime(2026, 11, 2, 9, tzinfo=UTC)


def test_the_alert_is_once_per_account_and_month_even_under_concurrency(
    db: Session, migrated_engine: Engine, make_customer: Callable[..., Actor]
) -> None:
    alice, other = make_customer(), make_customer()
    account_id = alice.account_id
    assert account_id is not None
    workers = 4
    barrier = threading.Barrier(workers)

    def send(now: datetime, wait: bool) -> bool:
        with Session(migrated_engine, expire_on_commit=False) as session:
            if wait:
                barrier.wait()
            added = _send(session, alice.user_id, account_id, now)
            session.commit()
            return added

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: send(NOVEMBER, True), range(workers)))
    assert results.count(True) == 1, results
    assert send(datetime(2026, 11, 30, 23, tzinfo=UTC), False) is False  # same month
    assert send(datetime(2026, 12, 1, 0, 5, tzinfo=UTC), False) is True  # a new month
    assert len(_low_balance_alerts(db, alice.user_id)) == 2
    assert _low_balance_alerts(db, other.user_id) == []


def test_a_concurrent_request_waits_for_the_first_alert_then_sends_nothing(
    db: Session, migrated_engine: Engine, make_customer: Callable[..., Actor]
) -> None:
    """Deterministic version of the race: the first request has inserted but not committed."""
    alice = make_customer()
    assert alice.account_id is not None
    first = Session(migrated_engine, expire_on_commit=False)
    try:
        assert _send(first, alice.user_id, alice.account_id, NOVEMBER) is True
        with Session(migrated_engine) as second:
            second.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(OperationalError):  # blocked behind the first, not a duplicate
                _send(second, alice.user_id, alice.account_id, NOVEMBER)
        first.commit()
    finally:
        first.close()
    with Session(migrated_engine) as third:
        assert _send(third, alice.user_id, alice.account_id, NOVEMBER) is False
        third.commit()
    assert len(_low_balance_alerts(db, alice.user_id)) == 1
