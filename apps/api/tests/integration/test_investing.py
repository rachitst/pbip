"""ADR-0058: simulated investing in funds, stocks and bonds through the ledger (PostgreSQL)."""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.calendar import bank_today
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.core.errors import DomainError
from pbip_api.modules.accounts.models import Account, AccountType
from pbip_api.modules.analytics import repository as analytics_repository
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import (
    EntryDirection,
    IdempotencyKey,
    Transaction,
    TransactionEntry,
    TransactionType,
)
from pbip_api.modules.notifications.models import Notification, OutboxEvent
from pbip_api.modules.notifications.service import relay_outbox
from pbip_api.modules.wealth import investing_service, pricing
from pbip_api.modules.wealth.models import (
    InvestmentHolding,
    InvestmentInstrument,
    InvestmentOrder,
)
from pbip_api.modules.wealth.schemas import BuyRequest, SellRequest
from pbip_test_support import Actor, FakeClock
from sqlalchemy import Engine, case, func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

MF_LIQUID = "50000000-0000-4000-a000-000000000001"
MF_INDEX = "50000000-0000-4000-a000-000000000003"
STK_SIMINFRA = "50000000-0000-4000-a000-000000000004"
BND_GSEC = "50000000-0000-4000-a000-000000000007"
FUNDS = 2_00_000_00  # INR 2 lakh
AMOUNT = 40_000_00
INVESTMENTS = ledger.SYSTEM_INVESTMENTS_ID
MICRO = pricing.MICRO_PER_UNIT


def _today(clock: FakeClock) -> date:
    return bank_today(datetime.fromtimestamp(clock(), tz=UTC))


def _price(db: Session, instrument_id: str, day: date) -> int:
    instrument = db.get(InvestmentInstrument, uuid.UUID(instrument_id))
    assert instrument is not None
    return pricing.instrument_price(instrument, day)


def _buy(
    client: TestClient,
    actor: Actor,
    instrument: str = MF_INDEX,
    *,
    amount: int | None = AMOUNT,
    units: int | None = None,
    key: str | None = None,
    account_id: uuid.UUID | str | None = None,
) -> Any:
    body: dict[str, Any] = {
        "instrument_id": instrument,
        "account_id": str(account_id or actor.account_id),
    }
    if amount is not None:
        body["amount_minor"] = amount
    if units is not None:
        body["units"] = units
    return client.post(
        "/api/v1/wealth/investments/buy",
        headers={**actor.headers, "Idempotency-Key": key or uuid.uuid4().hex},
        json=body,
    )


def _sell(
    client: TestClient,
    actor: Actor,
    holding_id: str,
    units_micro: int,
    *,
    key: str | None = None,
    account_id: uuid.UUID | str | None = None,
) -> Any:
    return client.post(
        f"/api/v1/wealth/holdings/{holding_id}/sell",
        headers={**actor.headers, "Idempotency-Key": key or uuid.uuid4().hex},
        json={"account_id": str(account_id or actor.account_id), "units_micro": units_micro},
    )


def _balance(session: Session, account_id: uuid.UUID | None) -> int:
    account = session.get(Account, account_id, populate_existing=True)
    assert account is not None
    return account.ledger_balance


def _transactions(session: Session, type_: TransactionType) -> list[Transaction]:
    return list(session.execute(select(Transaction).where(Transaction.type == type_)).scalars())


def _count(session: Session, model: type[Any]) -> int:
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def _holding(session: Session, holding_id: str) -> InvestmentHolding:
    holding = session.get(InvestmentHolding, uuid.UUID(holding_id), populate_existing=True)
    assert holding is not None
    return holding


def _assert_ledger_consistent(session: Session) -> None:
    """Every transaction balances, every materialised balance equals its entries, and the
    settlement account equals purchases minus sales over the order rows (ADR-0058 section 4)."""
    signed = case(
        (TransactionEntry.direction == EntryDirection.CREDIT, TransactionEntry.amount),
        else_=-TransactionEntry.amount,
    )
    unbalanced = session.execute(
        select(TransactionEntry.transaction_id)
        .group_by(TransactionEntry.transaction_id)
        .having(func.sum(signed) != 0)
    ).all()
    assert unbalanced == []
    for account in session.execute(
        select(Account).execution_options(populate_existing=True)
    ).scalars():
        assert ledger.entry_balance(session, account.id) == account.ledger_balance
    net = case(
        (InvestmentOrder.side == "BUY", InvestmentOrder.amount_minor),
        else_=-InvestmentOrder.amount_minor,
    )
    orders_net = session.execute(select(func.coalesce(func.sum(net), 0))).scalar_one()
    assert _balance(session, INVESTMENTS) == int(orders_net)


def _no_order_state(session: Session) -> None:
    assert _count(session, InvestmentOrder) == 0
    assert _transactions(session, TransactionType.INVESTMENT_PURCHASE) == []
    assert _transactions(session, TransactionType.INVESTMENT_SALE) == []
    assert (
        session.execute(
            select(func.count())
            .select_from(InvestmentHolding)
            .where(InvestmentHolding.units_micro > 0)
        ).scalar_one()
        == 0
    )


# ------------------------------------------------------------------------- instruments
def test_instruments_are_funds_stocks_and_bonds_with_a_simulated_daily_price(
    client: TestClient, make_customer: Callable[..., Actor], clock: FakeClock, db: Session
) -> None:
    alice = make_customer()
    resp = client.get("/api/v1/wealth/instruments", headers=alice.headers)
    assert resp.status_code == 200, resp.text
    by_code = {i["code"]: i for i in resp.json()}
    assert len(by_code) == 8
    assert {i["kind"] for i in by_code.values()} == {"MUTUAL_FUND", "STOCK", "BOND"}
    assert "MF-ELSS" not in by_code and "SGB" not in by_code  # lock-ins need lots (ADR-0058)
    today = _today(clock)
    for item in by_code.values():
        assert item["price_source"] == "SIMULATED" and item["price_date"] == today.isoformat()
        assert item["price_minor"] == _price(db, item["id"], today) > 0
    assert by_code["MF-INDEX"]["order_mode"] == "AMOUNT"
    assert by_code["MF-INDEX"]["wealth_product_id"] == "20000000-0000-4000-a000-000000000006"
    assert by_code["MF-INDEX"]["min_order_minor"] == 500_00
    assert by_code["STK-SIMINFRA"]["order_mode"] == "UNITS"
    assert by_code["STK-SIMINFRA"]["min_order_minor"] is None
    assert "fictional" in by_code["STK-SIMINFRA"]["name"].lower()


def test_the_price_is_the_same_all_day_and_moves_between_days(
    client: TestClient, make_customer: Callable[..., Actor], clock: FakeClock
) -> None:
    alice = make_customer()

    def stock_price() -> int:
        rows = client.get("/api/v1/wealth/instruments", headers=alice.headers).json()
        return int(next(i["price_minor"] for i in rows if i["code"] == "STK-SIMSOFT"))

    first = stock_price()
    assert stock_price() == first
    prices = set()
    for _ in range(5):
        clock.advance(86_400)
        prices.add(stock_price())
    assert len(prices) > 1


def test_investing_routes_are_customer_only(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    assert client.get("/api/v1/wealth/instruments").status_code == 401
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    for path in ["/api/v1/wealth/instruments", "/api/v1/wealth/holdings"]:
        assert client.get(path, headers=analyst.headers).status_code == 403, path
    assert _buy(client, analyst, account_id=uuid.uuid4()).status_code == 403
    assert (
        _sell(client, analyst, str(uuid.uuid4()), MICRO, account_id=uuid.uuid4()).status_code == 403
    )


# ---------------------------------------------------------------------------- buying
def test_buying_a_fund_debits_the_amount_and_credits_rounded_down_units(
    client: TestClient, make_customer: Callable[..., Actor], clock: FakeClock, db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    price = _price(db, MF_INDEX, _today(clock))
    resp = _buy(client, alice, MF_INDEX, amount=AMOUNT)
    assert resp.status_code == 201, resp.text
    order = resp.json()
    expected_units = AMOUNT * MICRO // price
    assert order["side"] == "BUY" and order["units_micro"] == expected_units
    assert order["price_minor"] == price and order["price_source"] == "SIMULATED"
    assert order["amount_minor"] == order["cost_basis_minor"] == AMOUNT
    assert order["realised_gain_minor"] is None

    (posting,) = _transactions(db, TransactionType.INVESTMENT_PURCHASE)
    assert str(posting.id) == order["transaction_id"] and posting.amount == AMOUNT
    assert posting.debit_account_id == alice.account_id
    assert posting.credit_account_id == INVESTMENTS
    assert _balance(db, alice.account_id) == FUNDS - AMOUNT
    assert _balance(db, INVESTMENTS) == AMOUNT
    holding = _holding(db, order["holding_id"])
    assert (holding.units_micro, holding.cost_basis_minor) == (expected_units, AMOUNT)

    audit = db.execute(select(AuditLog).where(AuditLog.action == "INVESTMENT_BOUGHT")).scalar_one()
    assert audit.actor_user_id == alice.user_id and audit.resource_id == order["id"]
    assert audit.details is not None and audit.details["price_source"] == "SIMULATED"
    assert (
        db.execute(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == "INVESTMENT_BOUGHT")
        ).scalar_one()
        == 1
    )
    relay_outbox(db)
    db.commit()
    note = db.execute(
        select(Notification).where(Notification.type == "INVESTMENT_BOUGHT")
    ).scalar_one()
    assert note.user_id == alice.user_id and note.resource_id == posting.id
    assert note.title == "Investment bought" and "INR 40,000.00" in note.body

    listed = client.get("/api/v1/wealth/holdings", headers=alice.headers).json()
    assert [h["id"] for h in listed] == [order["holding_id"]]
    assert listed[0]["value_minor"] == expected_units * price // MICRO
    assert listed[0]["gain_minor"] == listed[0]["value_minor"] - AMOUNT  # rounding: <= 0 today
    _assert_ledger_consistent(db)


def test_buying_stocks_and_bonds_takes_whole_units_at_the_day_price(
    client: TestClient, make_customer: Callable[..., Actor], clock: FakeClock, db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    today = _today(clock)
    stock = _buy(client, alice, STK_SIMINFRA, amount=None, units=7)
    bond = _buy(client, alice, BND_GSEC, amount=None, units=3)
    assert stock.status_code == bond.status_code == 201, (stock.text, bond.text)
    assert stock.json()["amount_minor"] == 7 * _price(db, STK_SIMINFRA, today)
    assert stock.json()["units_micro"] == 7 * MICRO
    assert bond.json()["amount_minor"] == 3 * _price(db, BND_GSEC, today)
    spent = stock.json()["amount_minor"] + bond.json()["amount_minor"]
    assert _balance(db, alice.account_id) == FUNDS - spent

    p = client.get("/api/v1/wealth/portfolio", headers=alice.headers).json()
    assert p["stocks_minor"] == stock.json()["amount_minor"]  # same day: value = cost
    assert p["bonds_minor"] == bond.json()["amount_minor"]
    assert p["funds_minor"] == 0 and p["investments_cost_minor"] == spent
    assert p["investments_gain_minor"] == 0
    assert p["total_minor"] == p["cash_minor"] + p["stocks_minor"] + p["bonds_minor"]
    shares = {s["asset_class"]: s["share_bps"] for s in p["allocation"]}
    assert sum(shares.values()) == 10_000 and shares["STOCKS"] > 0 and shares["BONDS"] > 0
    _assert_ledger_consistent(db)


def test_invalid_purchases_are_refused_and_leave_no_state(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    cases = {
        "amount and units": _buy(client, alice, MF_INDEX, amount=AMOUNT, units=1),
        "neither": _buy(client, alice, MF_INDEX, amount=None, units=None),
        "units for a fund": _buy(client, alice, MF_INDEX, amount=None, units=3),
        "amount for a stock": _buy(client, alice, STK_SIMINFRA, amount=AMOUNT),
        "below the fund minimum": _buy(client, alice, MF_INDEX, amount=499_99),
        "zero amount": _buy(client, alice, MF_INDEX, amount=0),
        "negative units": _buy(client, alice, STK_SIMINFRA, amount=None, units=-1),
        "too many units": _buy(client, alice, STK_SIMINFRA, amount=None, units=100_001),
    }
    for name, resp in cases.items():
        assert resp.status_code == 422, f"{name}: {resp.status_code} {resp.text}"
    unknown = _buy(client, alice, str(uuid.uuid4()))
    assert unknown.status_code == 404
    extra = client.post(
        "/api/v1/wealth/investments/buy",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={"instrument_id": MF_INDEX, "account_id": str(alice.account_id),
              "amount_minor": AMOUNT, "price_minor": 1},
    )  # fmt: skip
    assert extra.status_code == 422  # no price from the client
    no_key = client.post(
        "/api/v1/wealth/investments/buy",
        headers=alice.headers,
        json={"instrument_id": MF_INDEX, "account_id": str(alice.account_id),
              "amount_minor": AMOUNT},
    )  # fmt: skip
    assert no_key.status_code == 422
    _no_order_state(db)
    assert _count(db, IdempotencyKey) == 0
    assert _balance(db, alice.account_id) == FUNDS


def test_insufficient_funds_is_422_and_leaves_no_partial_state(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=10_000_00)
    resp = _buy(client, alice, MF_INDEX, amount=20_000_00)
    assert resp.status_code == 422 and resp.json()["code"] == "insufficient_funds"
    _no_order_state(db)
    assert _count(db, InvestmentHolding) == 0  # the zero holding was rolled back too
    assert _count(db, IdempotencyKey) == 0
    assert _balance(db, alice.account_id) == 10_000_00 and _balance(db, INVESTMENTS) == 0


def test_idempotent_retry_replays_and_debits_once(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    key = uuid.uuid4().hex
    first, retry = _buy(client, alice, key=key), _buy(client, alice, key=key)
    assert first.status_code == retry.status_code == 201
    assert retry.headers.get("Idempotent-Replayed") == "true" and retry.json() == first.json()
    assert _count(db, InvestmentOrder) == 1
    assert _balance(db, alice.account_id) == FUNDS - AMOUNT
    reused = _buy(client, alice, amount=AMOUNT + 1, key=key)
    assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reuse"
    sold_with_buy_key = _sell(client, alice, first.json()["holding_id"], 1, key=key)
    assert sold_with_buy_key.json()["code"] == "idempotency_key_reuse"


# --------------------------------------------------------------------------- selling
def test_a_partial_sale_credits_rounded_down_proceeds_and_moves_average_cost(
    client: TestClient, make_customer: Callable[..., Actor], clock: FakeClock, db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    bought = _buy(client, alice, MF_INDEX, amount=AMOUNT).json()
    held = bought["units_micro"]
    clock.advance(40 * 86_400)
    price = _price(db, MF_INDEX, _today(clock))
    sell_units = held // 3
    resp = _sell(client, alice, bought["holding_id"], sell_units)
    assert resp.status_code == 200, resp.text
    sale = resp.json()
    proceeds = sell_units * price // MICRO
    cost = AMOUNT * sell_units // held
    assert sale["side"] == "SELL" and sale["amount_minor"] == proceeds
    assert sale["cost_basis_minor"] == cost and sale["realised_gain_minor"] == proceeds - cost
    (posting,) = _transactions(db, TransactionType.INVESTMENT_SALE)
    assert posting.debit_account_id == INVESTMENTS
    assert posting.credit_account_id == alice.account_id and posting.amount == proceeds
    assert _balance(db, alice.account_id) == FUNDS - AMOUNT + proceeds
    holding = _holding(db, bought["holding_id"])
    assert (holding.units_micro, holding.cost_basis_minor) == (held - sell_units, AMOUNT - cost)
    audit = db.execute(select(AuditLog).where(AuditLog.action == "INVESTMENT_SOLD")).scalar_one()
    assert audit.details is not None and audit.details["realised_gain_minor"] == proceeds - cost
    relay_outbox(db)
    db.commit()
    note = db.execute(
        select(Notification).where(Notification.type == "INVESTMENT_SOLD")
    ).scalar_one()
    assert note.resource_id == posting.id and note.title == "Investment sold"
    _assert_ledger_consistent(db)


def test_selling_everything_clears_the_cost_and_hides_the_holding(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    bought = _buy(client, alice, STK_SIMINFRA, amount=None, units=5).json()
    resp = _sell(client, alice, bought["holding_id"], 5 * MICRO)
    assert resp.status_code == 200, resp.text
    assert resp.json()["cost_basis_minor"] == bought["amount_minor"]
    assert resp.json()["realised_gain_minor"] == 0  # same day, same price
    holding = _holding(db, bought["holding_id"])
    assert (holding.units_micro, holding.cost_basis_minor) == (0, 0)
    assert client.get("/api/v1/wealth/holdings", headers=alice.headers).json() == []
    assert _balance(db, alice.account_id) == FUNDS
    _assert_ledger_consistent(db)


def test_selling_more_than_held_or_part_of_a_stock_is_refused_and_moves_nothing(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    bought = _buy(client, alice, STK_SIMINFRA, amount=None, units=2).json()
    balance = _balance(db, alice.account_id)
    too_many = _sell(client, alice, bought["holding_id"], 3 * MICRO)
    assert too_many.status_code == 422 and too_many.json()["code"] == "insufficient_units"
    part = _sell(client, alice, bought["holding_id"], MICRO // 2)
    assert part.status_code == 422
    assert _transactions(db, TransactionType.INVESTMENT_SALE) == []
    assert _count(db, InvestmentOrder) == 1
    assert _holding(db, bought["holding_id"]).units_micro == 2 * MICRO
    assert _balance(db, alice.account_id) == balance


def test_a_sale_worth_less_than_a_paisa_is_refused(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    bought = _buy(client, alice, MF_INDEX, amount=AMOUNT).json()
    resp = _sell(client, alice, bought["holding_id"], 1)  # one micro-unit of a ~INR 220 NAV
    assert resp.status_code == 422 and resp.json()["code"] == "order_too_small"
    assert _transactions(db, TransactionType.INVESTMENT_SALE) == []


# ----------------------------------------------------------------------- concurrency
def _buy_in_thread(
    engine: Engine,
    settings: Settings,
    actor: Actor,
    key: str,
    amount: int,
    barrier: threading.Barrier,
) -> str:
    assert actor.customer_id is not None and actor.account_id is not None
    with Session(engine, expire_on_commit=False) as session:
        barrier.wait()
        try:
            result = investing_service.buy(
                session,
                settings,
                customer_id=actor.customer_id,
                user_id=actor.user_id,
                request=BuyRequest(
                    instrument_id=uuid.UUID(MF_LIQUID),
                    account_id=actor.account_id,
                    amount_minor=amount,
                ),
                idempotency_key=key,
                meta=RequestMeta(),
                now=utcnow(),
            )
        except DomainError as exc:
            session.rollback()
            return exc.code
        return "replayed" if result.replayed else "created"


def test_concurrent_purchases_against_one_balance_never_overdraw(
    make_customer: Callable[..., Actor], migrated_engine: Engine, settings: Settings, db: Session
) -> None:
    """Real threads, real PostgreSQL: six INR 40,000 purchases race for INR 1 lakh; exactly two
    fit, the holding has exactly their units and the account never goes below zero."""
    alice = make_customer(funds=1_00_000_00)
    workers = 6
    barrier = threading.Barrier(workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(
            pool.map(
                lambda _: _buy_in_thread(
                    migrated_engine, settings, alice, uuid.uuid4().hex, AMOUNT, barrier
                ),
                range(workers),
            )
        )
    assert sorted(outcomes) == ["created"] * 2 + ["insufficient_funds"] * 4, outcomes
    assert _balance(db, alice.account_id) == 1_00_000_00 - 2 * AMOUNT >= 0
    orders = list(db.execute(select(InvestmentOrder)).scalars())
    (holding,) = db.execute(select(InvestmentHolding)).scalars()
    db.refresh(holding)
    assert holding.units_micro == sum(o.units_micro for o in orders)
    assert holding.cost_basis_minor == 2 * AMOUNT
    _assert_ledger_consistent(db)


def test_concurrent_duplicate_purchases_execute_exactly_once(
    make_customer: Callable[..., Actor], migrated_engine: Engine, settings: Settings, db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    workers, key = 6, uuid.uuid4().hex
    barrier = threading.Barrier(workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(
            pool.map(
                lambda _: _buy_in_thread(migrated_engine, settings, alice, key, AMOUNT, barrier),
                range(workers),
            )
        )
    assert sorted(outcomes) == ["created"] + ["replayed"] * (workers - 1)
    assert _count(db, InvestmentOrder) == 1
    assert _balance(db, alice.account_id) == FUNDS - AMOUNT
    _assert_ledger_consistent(db)


def test_concurrent_sales_never_sell_more_than_held(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    settings: Settings,
    db: Session,
) -> None:
    alice = make_customer(funds=FUNDS)
    bought = _buy(client, alice, STK_SIMINFRA, amount=None, units=5).json()
    workers = 6
    barrier = threading.Barrier(workers)
    assert alice.customer_id is not None and alice.account_id is not None
    customer_id, account_id = alice.customer_id, alice.account_id

    def sell_two(_: int) -> str:
        with Session(migrated_engine, expire_on_commit=False) as session:
            barrier.wait()
            try:
                investing_service.sell(
                    session,
                    settings,
                    customer_id=customer_id,
                    user_id=alice.user_id,
                    holding_id=uuid.UUID(bought["holding_id"]),
                    request=SellRequest(account_id=account_id, units_micro=2 * MICRO),
                    idempotency_key=uuid.uuid4().hex,
                    meta=RequestMeta(),
                    now=utcnow(),
                )
            except DomainError as exc:
                session.rollback()
                return exc.code
            return "sold"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(sell_two, range(workers)))
    assert sorted(outcomes) == ["insufficient_units"] * 4 + ["sold"] * 2, outcomes
    assert _holding(db, bought["holding_id"]).units_micro == MICRO
    assert len(_transactions(db, TransactionType.INVESTMENT_SALE)) == 2
    _assert_ledger_consistent(db)


# ------------------------------------------------------------------------- analytics
def test_investing_is_neither_spending_nor_income_in_analytics(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    """A purchase moves the customer's own money into their holdings (ADR-0058 section 4)."""
    alice = make_customer(salary=FUNDS // 2)
    bought = _buy(client, alice, STK_SIMINFRA, amount=None, units=4).json()
    assert _sell(client, alice, bought["holding_id"], 2 * MICRO).status_code == 200
    assert alice.customer_id is not None and alice.account_id is not None
    owned = analytics_repository.owned_account_ids(db, alice.customer_id)
    metrics = analytics_repository.get_financial_health_metrics(
        db, alice.account_id, owned, utcnow()
    )
    assert (metrics.income_minor, metrics.spend_minor) == (FUNDS, 0)


def test_a_sale_at_a_gain_posts_and_the_settlement_account_may_go_below_zero(
    client: TestClient, make_customer: Callable[..., Actor], clock: FakeClock, db: Session
) -> None:
    """SYS-INVESTMENTS is a SYSTEM account (migration 0031), the only type the accounts CHECK lets
    go below zero: the simulated market pays the gain (ADR-0058 section 4)."""
    settlement = db.get(Account, INVESTMENTS)
    assert settlement is not None and settlement.account_type == AccountType.SYSTEM
    alice = make_customer(funds=FUNDS)
    bought = _buy(client, alice, STK_SIMINFRA, amount=None, units=10).json()
    # The price is deterministic: walk forward to the first day it is above the purchase price.
    for _ in range(366):
        clock.advance(86_400)
        if _price(db, STK_SIMINFRA, _today(clock)) > bought["price_minor"]:
            break
    else:
        pytest.fail("the simulated price never rose above the purchase price within a year")
    resp = _sell(client, alice, bought["holding_id"], 10 * MICRO)
    assert resp.status_code == 200, resp.text
    sale = resp.json()
    assert sale["realised_gain_minor"] > 0
    assert sale["amount_minor"] == bought["amount_minor"] + sale["realised_gain_minor"]
    assert _balance(db, alice.account_id) == FUNDS + sale["realised_gain_minor"]
    assert _balance(db, INVESTMENTS) == -sale["realised_gain_minor"] < 0
    _assert_ledger_consistent(db)
