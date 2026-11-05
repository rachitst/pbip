"""ADR-0058 section 7: fund SIPs, a monthly purchase run by the beat job (PostgreSQL)."""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.calendar import BANK_TZ
from pbip_api.core.config import Settings
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import Transaction, TransactionType
from pbip_api.modules.notifications.models import Notification
from pbip_api.modules.notifications.service import relay_outbox
from pbip_api.modules.wealth import sip_service
from pbip_api.modules.wealth.models import FundSip, FundSipRun, InvestmentOrder
from pbip_test_support import Actor
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

MF_LIQUID = "50000000-0000-4000-a000-000000000001"
STK_SIMINFRA = "50000000-0000-4000-a000-000000000004"
FUNDS = 1_00_000_00
SIP_AMOUNT = 5_000_00
INVESTMENTS = ledger.SYSTEM_INVESTMENTS_ID


def _create(
    client: TestClient,
    actor: Actor,
    *,
    instrument: str = MF_LIQUID,
    amount: int = SIP_AMOUNT,
    day: int = 10,
    key: str | None = None,
    account_id: uuid.UUID | str | None = None,
) -> Any:
    return client.post(
        "/api/v1/wealth/sips",
        headers={**actor.headers, "Idempotency-Key": key or uuid.uuid4().hex},
        json={
            "instrument_id": instrument,
            "account_id": str(account_id or actor.account_id),
            "amount_minor": amount,
            "day_of_month": day,
        },
    )


def _noon(day: date) -> datetime:
    """Midday in India on ``day``, as an aware UTC instant."""
    return datetime.combine(day, time(12, 0), BANK_TZ).astimezone(UTC)


def _sip(db: Session, sip_id: str) -> FundSip:
    sip = db.get(FundSip, uuid.UUID(sip_id), populate_existing=True)
    assert sip is not None
    return sip


def _balance(db: Session, account_id: uuid.UUID | None) -> int:
    account = db.get(Account, account_id, populate_existing=True)
    assert account is not None
    return account.ledger_balance


def _count(db: Session, model: type[Any]) -> int:
    return int(db.execute(select(func.count()).select_from(model)).scalar_one())


def _purchases(db: Session) -> list[Transaction]:
    stmt = select(Transaction).where(Transaction.type == TransactionType.INVESTMENT_PURCHASE)
    return list(db.execute(stmt).scalars())


# ------------------------------------------------------------------------- dates (unit-like)
@pytest.mark.parametrize(
    ("day", "today", "expected"),
    [
        (10, date(2026, 11, 5), date(2026, 11, 10)),
        (5, date(2026, 11, 5), date(2026, 11, 5)),  # today's day: runs today
        (3, date(2026, 11, 5), date(2026, 12, 3)),
        (28, date(2026, 12, 30), date(2027, 1, 28)),
    ],
)
def test_first_run_on(day: int, today: date, expected: date) -> None:
    assert sip_service.first_run_on(day, today) == expected


@pytest.mark.parametrize(
    ("day", "after", "expected"),
    [
        (10, date(2026, 11, 10), date(2026, 12, 10)),
        (10, date(2026, 11, 9), date(2026, 11, 10)),
        (28, date(2027, 1, 31), date(2027, 2, 28)),
        (1, date(2026, 12, 15), date(2027, 1, 1)),
    ],
)
def test_next_run_after(day: int, after: date, expected: date) -> None:
    assert sip_service.next_run_after(day, after) == expected


# --------------------------------------------------------------------------- create/cancel
def test_creating_a_sip_moves_no_money_and_is_idempotent(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    key = uuid.uuid4().hex
    first, retry = _create(client, alice, key=key), _create(client, alice, key=key)
    assert first.status_code == retry.status_code == 201, first.text
    assert retry.headers.get("Idempotent-Replayed") == "true" and retry.json() == first.json()
    body = first.json()
    assert body["status"] == "ACTIVE" and body["amount_minor"] == SIP_AMOUNT
    assert body["day_of_month"] == 10 and body["runs"] == [] and body["next_run_on"]
    assert date.fromisoformat(body["next_run_on"]).day == 10
    assert _count(db, FundSip) == 1 and _purchases(db) == []
    assert _balance(db, alice.account_id) == FUNDS
    audit = db.execute(select(AuditLog).where(AuditLog.action == "FUND_SIP_CREATED")).scalar_one()
    assert audit.actor_user_id == alice.user_id and audit.resource_id == body["id"]
    listed = client.get("/api/v1/wealth/sips", headers=alice.headers).json()
    assert [s["id"] for s in listed] == [body["id"]]


def test_invalid_sips_are_refused(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    stock = _create(client, alice, instrument=STK_SIMINFRA)
    assert stock.status_code == 422 and stock.json()["code"] == "not_a_fund"
    for name, resp in {
        "below the fund minimum": _create(client, alice, amount=499_99),
        "day 0": _create(client, alice, day=0),
        "day 29": _create(client, alice, day=29),
        "zero amount": _create(client, alice, amount=0),
    }.items():
        assert resp.status_code == 422, f"{name}: {resp.status_code}"
    assert _create(client, alice, instrument=str(uuid.uuid4())).status_code == 404
    no_key = client.post(
        "/api/v1/wealth/sips",
        headers=alice.headers,
        json={"instrument_id": MF_LIQUID, "account_id": str(alice.account_id),
              "amount_minor": SIP_AMOUNT, "day_of_month": 10},
    )  # fmt: skip
    assert no_key.status_code == 422
    assert _count(db, FundSip) == 0


def test_cancelling_stops_future_instalments_and_is_safe_to_repeat(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, migrated_engine: Engine,
    settings: Settings,
) -> None:  # fmt: skip
    alice = make_customer(funds=FUNDS)
    sip = _create(client, alice).json()
    for _ in range(2):
        resp = client.post(f"/api/v1/wealth/sips/{sip['id']}/cancel", headers=alice.headers)
        assert resp.status_code == 200 and resp.json()["status"] == "CANCELLED"
        assert resp.json()["next_run_on"] is None
    assert (
        db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "FUND_SIP_CANCELLED")
        ).scalar_one()
        == 1
    )
    due = date.fromisoformat(sip["next_run_on"])
    counts = sip_service.process_due_sips(migrated_engine, now=_noon(due), settings=settings)
    assert sum(counts.values()) == 0 and _purchases(db) == []


# ---------------------------------------------------------------------------- the runner
def test_a_due_instalment_buys_once_and_moves_to_next_month(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, migrated_engine: Engine,
    settings: Settings,
) -> None:  # fmt: skip
    alice = make_customer(funds=FUNDS)
    sip = _create(client, alice).json()
    due = date.fromisoformat(sip["next_run_on"])
    counts = sip_service.process_due_sips(migrated_engine, now=_noon(due), settings=settings)
    assert counts["EXECUTED"] == 1 and sum(counts.values()) == 1
    again = sip_service.process_due_sips(migrated_engine, now=_noon(due), settings=settings)
    assert sum(again.values()) == 0  # nothing is due any more that day

    (purchase,) = _purchases(db)
    assert purchase.amount == SIP_AMOUNT and purchase.channel == "SCHEDULED"
    assert (
        purchase.debit_account_id == alice.account_id and purchase.credit_account_id == INVESTMENTS
    )
    (order,) = db.execute(select(InvestmentOrder)).scalars()
    assert str(order.sip_id) == sip["id"] and order.transaction_id == purchase.id
    (run,) = db.execute(select(FundSipRun)).scalars()
    assert run.run_on == due and run.status == "EXECUTED" and run.order_id == order.id
    row = _sip(db, sip["id"])
    assert row.next_run_on == sip_service.next_run_after(10, due) > due
    assert _balance(db, alice.account_id) == FUNDS - SIP_AMOUNT
    audit = db.execute(
        select(AuditLog).where(AuditLog.action == "FUND_SIP_INSTALMENT_BOUGHT")
    ).scalar_one()
    assert audit.actor_user_id is None and audit.details["actor"] == "scheduler"
    shown = client.get(f"/api/v1/wealth/sips/{sip['id']}", headers=alice.headers).json()
    assert [(r["status"], r["order_id"]) for r in shown["runs"]] == [("EXECUTED", str(order.id))]
    holdings = client.get("/api/v1/wealth/holdings", headers=alice.headers).json()
    assert [h["units_micro"] for h in holdings] == [order.units_micro]


def test_concurrent_runs_buy_each_instalment_exactly_once(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, migrated_engine: Engine,
    settings: Settings,
) -> None:  # fmt: skip
    """Real threads, real PostgreSQL: four overlapping job runs (a re-delivered task)."""
    alice = make_customer(funds=FUNDS)
    sip = _create(client, alice).json()
    moment = _noon(date.fromisoformat(sip["next_run_on"]))
    workers = 4
    barrier = threading.Barrier(workers)

    def run(_: int) -> str:
        with Session(migrated_engine, expire_on_commit=False) as session:
            barrier.wait()
            return sip_service.run_sip(session, settings, uuid.UUID(sip["id"]), moment).value

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(run, range(workers)))
    assert outcomes.count("EXECUTED") == 1, outcomes
    assert set(outcomes) <= {"EXECUTED", "SKIPPED", "REPLAY"}
    assert len(_purchases(db)) == 1 and _count(db, FundSipRun) == 1
    assert _balance(db, alice.account_id) == FUNDS - SIP_AMOUNT


def test_a_refused_instalment_is_recorded_notified_and_skipped_to_next_month(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, migrated_engine: Engine,
    settings: Settings,
) -> None:  # fmt: skip
    alice = make_customer(funds=1_000_00)  # INR 1,000: less than the INR 5,000 instalment
    sip = _create(client, alice).json()
    due = date.fromisoformat(sip["next_run_on"])
    counts = sip_service.process_due_sips(migrated_engine, now=_noon(due), settings=settings)
    assert counts["FAILED"] == 1
    assert _purchases(db) == [] and _count(db, InvestmentOrder) == 0
    assert _balance(db, alice.account_id) == 1_000_00
    (run,) = db.execute(select(FundSipRun)).scalars()
    assert (run.status, run.failure_reason, run.order_id) == ("FAILED", "insufficient_funds", None)
    assert _sip(db, sip["id"]).next_run_on > due
    relay_outbox(db)
    db.commit()
    note = db.execute(
        select(Notification).where(Notification.type == "FUND_SIP_INSTALMENT_FAILED")
    ).scalar_one()
    assert note.user_id == alice.user_id and "INR 5,000.00" in note.body


def test_missed_months_are_not_caught_up(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, migrated_engine: Engine,
    settings: Settings,
) -> None:  # fmt: skip
    alice = make_customer(funds=FUNDS)
    sip = _create(client, alice).json()
    late = date.fromordinal(date.fromisoformat(sip["next_run_on"]).toordinal() + 70)
    counts = sip_service.process_due_sips(migrated_engine, now=_noon(late), settings=settings)
    assert counts["EXECUTED"] == 1 and len(_purchases(db)) == 1
    assert _sip(db, sip["id"]).next_run_on > late
    assert (
        sum(
            sip_service.process_due_sips(
                migrated_engine, now=_noon(late), settings=settings
            ).values()
        )
        == 0
    )
