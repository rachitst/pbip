"""TASK-063/064 / ADR-0025: wealth catalogue, fixed-deposit booking and closure (PostgreSQL)."""

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
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.analytics import repository as analytics_repository
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.customers.models import Customer
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
from pbip_api.modules.payments.service import PaymentsService, TransferCommand
from pbip_api.modules.wealth import deposit_math
from pbip_api.modules.wealth import service as wealth_service
from pbip_api.modules.wealth.models import FixedDeposit
from pbip_api.modules.wealth.schemas import DepositBookRequest
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.testing import FixedDecisionScorer
from pbip_test_support import Actor, FakeClock, age_payee
from sqlalchemy import Engine, case, func, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

FD_SHORT = "20000000-0000-4000-a000-000000000001"  # 6.50%, 6 or 9 months, penalty 1%
FD_REGULAR = "20000000-0000-4000-a000-000000000002"  # 7.10%, 12-36 months, penalty 1%
FD_TAX_SAVER = "20000000-0000-4000-a000-000000000003"  # 6.75%, 60 months, locked in
MF_INDEX = "20000000-0000-4000-a000-000000000006"
FUNDS = 2_00_000_00  # INR 2 lakh
AMOUNT = 1_00_000_00  # INR 1 lakh
DAY = 86_400
DEPOSITS = ledger.SYSTEM_FIXED_DEPOSITS_ID
CLEARING = ledger.SYSTEM_CLEARING_ID


def _terms(product: str = FD_REGULAR, amount: int = AMOUNT, months: int = 12) -> dict[str, Any]:
    return {"product_id": product, "amount_minor": amount, "tenure_months": months}


def _book(
    client: TestClient,
    actor: Actor,
    product: str = FD_REGULAR,
    amount: int = AMOUNT,
    months: int = 12,
    *,
    key: str | None = None,
    account_id: uuid.UUID | str | None = None,
) -> Any:
    return client.post(
        "/api/v1/wealth/fixed-deposits/book",
        headers={**actor.headers, "Idempotency-Key": key or uuid.uuid4().hex},
        json=_terms(product, amount, months) | {"account_id": str(account_id or actor.account_id)},
    )


def _close(client: TestClient, actor: Actor, deposit_id: str, *, key: str | None = None) -> Any:
    return client.post(
        f"/api/v1/wealth/fixed-deposits/{deposit_id}/close",
        headers={**actor.headers, "Idempotency-Key": key or uuid.uuid4().hex},
    )


def _balance(session: Session, account_id: uuid.UUID | None) -> int:
    account = session.get(Account, account_id, populate_existing=True)
    assert account is not None
    return account.ledger_balance


def _transactions(session: Session, type_: TransactionType) -> list[Transaction]:
    return list(session.execute(select(Transaction).where(Transaction.type == type_)).scalars())


def _count(session: Session, model: type[Any]) -> int:
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def _assert_ledger_consistent(session: Session) -> None:
    """Every transaction balances and every materialised balance equals its entries."""
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


def _open_principal(session: Session) -> int:
    total = session.execute(
        select(func.coalesce(func.sum(FixedDeposit.principal), 0)).where(
            FixedDeposit.status == "ACTIVE"
        )
    ).scalar_one()
    return int(total)


# ----------------------------------------------------------------- catalogue (TASK-063)
def test_catalogue_lists_deposits_funds_and_the_gold_bond_with_yield_and_lock_in(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    resp = client.get("/api/v1/wealth/products", headers=alice.headers)
    assert resp.status_code == 200, resp.text
    products = {p["code"]: p for p in resp.json()}
    assert {p["product_type"] for p in products.values()} == {
        "FIXED_DEPOSIT",
        "MUTUAL_FUND",
        "SOVEREIGN_GOLD_BOND",
    }
    regular = products["FD-REGULAR"]
    assert regular["annual_rate_bps"] == 710 and regular["apy_bps"] == 729  # quarterly compounding
    assert regular["tenures_months"] == [12, 18, 24, 36] and regular["min_lock_in_months"] == 0
    assert regular["bookable"] and regular["premature_closure_allowed"]
    tax = products["FD-TAX-SAVER"]
    assert tax["min_lock_in_months"] == 60 and not tax["premature_closure_allowed"]
    assert tax["max_amount_minor"] == 1_50_000_00
    for code in ("MF-LIQUID", "MF-BALANCED", "MF-INDEX", "MF-ELSS"):
        fund = products[code]  # no market data: no yield is invented
        assert fund["yield_basis"] == "MARKET_LINKED" and not fund["bookable"]
        assert fund["annual_rate_bps"] is None and fund["apy_bps"] is None
        assert fund["tenures_months"] == []
    assert products["MF-ELSS"]["min_lock_in_months"] == 36
    gold = products["SGB"]
    assert (gold["apy_bps"], gold["tenures_months"], gold["min_lock_in_months"]) == (250, [96], 60)
    assert {p["catalogue_version"] for p in products.values()} == {"2026-11-v1"}
    assert all(
        "simulated" in p["notes"] or "market" in p["notes"].lower() for p in products.values()
    )


@pytest.mark.parametrize(
    ("risk", "codes"),
    [
        ("LOW", {"FD-SHORT", "FD-REGULAR", "FD-TAX-SAVER", "MF-LIQUID"}),
        ("MODERATE", {"MF-BALANCED", "SGB"}),
        ("HIGH", {"MF-INDEX", "MF-ELSS"}),
    ],
)
def test_catalogue_filters_by_risk_level(
    client: TestClient, make_customer: Callable[..., Actor], risk: str, codes: set[str]
) -> None:
    alice = make_customer()
    resp = client.get(f"/api/v1/wealth/products?risk_level={risk}", headers=alice.headers)
    assert resp.status_code == 200
    assert {p["code"] for p in resp.json()} == codes
    assert {p["risk_level"] for p in resp.json()} == {risk}


def test_catalogue_rejects_an_unknown_risk_level(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    resp = client.get("/api/v1/wealth/products?risk_level=EXTREME", headers=alice.headers)
    assert resp.status_code == 422


def test_wealth_routes_are_customer_only(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    assert client.get("/api/v1/wealth/products").status_code == 401
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    for method, path in [
        ("get", "/api/v1/wealth/products"),
        ("get", "/api/v1/wealth/portfolio"),
        ("get", "/api/v1/wealth/fixed-deposits"),
        ("get", f"/api/v1/wealth/fixed-deposits/{uuid.uuid4()}"),
    ]:
        assert getattr(client, method)(path, headers=analyst.headers).status_code == 403, path
    assert _book(client, analyst, account_id=uuid.uuid4()).status_code == 403


# ------------------------------------------------------------------ booking (TASK-064)
def test_quote_is_exact_and_books_nothing(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, clock: FakeClock
) -> None:
    alice = make_customer(funds=FUNDS)
    resp = client.post("/api/v1/wealth/fixed-deposits/quote", headers=alice.headers, json=_terms())
    assert resp.status_code == 200, resp.text
    quote = resp.json()
    start = bank_today(datetime.fromtimestamp(clock(), tz=UTC))
    expected = deposit_math.maturity(AMOUNT, 710, 12, start)
    assert quote["start_date"] == start.isoformat()
    assert quote["maturity_date"] == expected.maturity_date.isoformat()
    assert quote["maturity_amount_minor"] == expected.maturity_amount_minor == 1_07_291_28
    assert quote["maturity_interest_minor"] == 7_291_28 and quote["apy_bps"] == 729
    assert _count(db, FixedDeposit) == 0 and _balance(db, alice.account_id) == FUNDS


def test_booking_posts_one_balanced_entry_and_stores_the_deposit(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    quote = client.post(
        "/api/v1/wealth/fixed-deposits/quote", headers=alice.headers, json=_terms()
    ).json()
    resp = _book(client, alice)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "ACTIVE" and body["principal_minor"] == AMOUNT
    assert body["annual_rate_bps"] == 710  # from the catalogue, never from the client
    for field in ("maturity_date", "maturity_amount_minor", "maturity_interest_minor"):
        assert body[field] == quote[field]  # the preview is exactly what is booked
    assert body["accrued_interest_minor"] == 0 and body["days_to_maturity"] > 360

    # Ledger: one FD_BOOKING, DEBIT customer / CREDIT the deposits liability account.
    (booking,) = _transactions(db, TransactionType.FD_BOOKING)
    assert str(booking.id) == body["booking_transaction_id"]
    assert (booking.debit_account_id, booking.credit_account_id) == (alice.account_id, DEPOSITS)
    assert _balance(db, alice.account_id) == FUNDS - AMOUNT
    assert _balance(db, DEPOSITS) == AMOUNT == _open_principal(db)  # reconciles
    _assert_ledger_consistent(db)

    deposit = db.execute(select(FixedDeposit)).scalar_one()
    assert deposit.maturity_amount == deposit.principal + deposit.maturity_interest
    assert deposit.customer_id == alice.customer_id and deposit.account_id == alice.account_id

    audit = db.execute(
        select(AuditLog).where(AuditLog.action == "FIXED_DEPOSIT_BOOKED")
    ).scalar_one()
    assert audit.resource_id == body["id"] and audit.actor_user_id == alice.user_id
    relay_outbox(db)
    db.commit()
    note = db.execute(
        select(Notification).where(Notification.type == "FIXED_DEPOSIT_BOOKED")
    ).scalar_one()
    assert note.user_id == alice.user_id and note.resource_id == booking.id
    assert note.title == "Fixed deposit booked" and "INR 1,07,291.28" in note.body

    listed = client.get("/api/v1/wealth/fixed-deposits", headers=alice.headers).json()
    assert [d["id"] for d in listed] == [body["id"]]
    detail = client.get(f"/api/v1/wealth/fixed-deposits/{body['id']}", headers=alice.headers)
    assert detail.status_code == 200 and detail.json()["maturity_date"] == body["maturity_date"]


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ({"amount_minor": 0}, 422, None),
        ({"amount_minor": 9_999_99}, 422, None),  # below the INR 10,000 minimum
        ({"amount_minor": 1_00_00_000_01}, 422, None),  # above the INR 1 crore maximum
        ({"tenure_months": 13}, 422, None),  # not offered
        ({"customer_id": str(uuid.uuid4())}, 422, None),  # never accepted from the client
        ({"annual_rate_bps": 2_000}, 422, None),  # the rate comes from the catalogue only
        ({"product_id": MF_INDEX, "tenure_months": 12}, 422, "product_not_bookable"),
        ({"product_id": str(uuid.uuid4())}, 404, None),
        ({"account_id": "not-a-uuid"}, 422, None),
    ],
)
def test_booking_rejects_invalid_requests_and_leaves_no_state(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
    change: dict[str, Any],
    status: int,
    code: str | None,
) -> None:
    alice = make_customer(funds=FUNDS)
    payload = _terms() | {"account_id": str(alice.account_id)} | change
    resp = client.post(
        "/api/v1/wealth/fixed-deposits/book",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json=payload,
    )
    assert resp.status_code == status, resp.text
    if code:
        assert resp.json()["code"] == code
    assert _count(db, FixedDeposit) == 0 and _transactions(db, TransactionType.FD_BOOKING) == []
    assert _balance(db, alice.account_id) == FUNDS


def test_booking_requires_an_idempotency_key(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    resp = client.post(
        "/api/v1/wealth/fixed-deposits/book",
        headers=alice.headers,
        json=_terms() | {"account_id": str(alice.account_id)},
    )
    assert resp.status_code == 422
    assert _count(db, FixedDeposit) == 0


def test_insufficient_funds_is_422_and_leaves_no_partial_state(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=50_000_00)
    resp = _book(client, alice, amount=AMOUNT)
    assert resp.status_code == 422 and resp.json()["code"] == "insufficient_funds"
    assert _count(db, FixedDeposit) == 0
    assert _transactions(db, TransactionType.FD_BOOKING) == []
    assert _count(db, IdempotencyKey) == 0  # rolled back: the same key may be retried
    assert (
        db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "FIXED_DEPOSIT_BOOKED")
        ).scalar_one()
        == 0
    )
    assert (
        db.execute(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == "FIXED_DEPOSIT_BOOKED")
        ).scalar_one()
        == 0
    )
    assert _balance(db, alice.account_id) == 50_000_00 and _balance(db, DEPOSITS) == 0


def test_idempotent_retry_returns_the_same_deposit_and_debits_once(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=FUNDS)
    key = uuid.uuid4().hex
    first, retry = _book(client, alice, key=key), _book(client, alice, key=key)
    assert first.status_code == retry.status_code == 201
    assert retry.headers.get("Idempotent-Replayed") == "true"
    assert retry.json() == first.json()
    assert _count(db, FixedDeposit) == 1 and len(_transactions(db, TransactionType.FD_BOOKING)) == 1
    assert _balance(db, alice.account_id) == FUNDS - AMOUNT
    reused = _book(client, alice, amount=AMOUNT + 1_000_00, key=key)  # same key, other request
    assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reuse"
    closed_with_book_key = _close(client, alice, first.json()["id"], key=key)  # other endpoint
    assert closed_with_book_key.json()["code"] == "idempotency_key_reuse"


def _book_in_thread(
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
            result = wealth_service.book_fixed_deposit(
                session,
                settings,
                customer_id=actor.customer_id,
                user_id=actor.user_id,
                request=DepositBookRequest(
                    product_id=uuid.UUID(FD_REGULAR),
                    amount_minor=amount,
                    tenure_months=12,
                    account_id=actor.account_id,
                ),
                idempotency_key=key,
                meta=RequestMeta(),
                now=utcnow(),
            )
        except DomainError as exc:
            session.rollback()
            return exc.code
        return "replayed" if result.replayed else "created"


def test_concurrent_duplicate_bookings_book_exactly_once(
    make_customer: Callable[..., Actor], migrated_engine: Engine, settings: Settings, db: Session
) -> None:
    """Real threads, real PostgreSQL: a double-clicked submit sent six times at once."""
    alice = make_customer(funds=FUNDS)
    workers, key = 6, uuid.uuid4().hex
    barrier = threading.Barrier(workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(
            pool.map(
                lambda _: _book_in_thread(migrated_engine, settings, alice, key, AMOUNT, barrier),
                range(workers),
            )
        )
    assert sorted(outcomes) == ["created"] + ["replayed"] * (workers - 1)
    assert _count(db, FixedDeposit) == 1
    assert len(_transactions(db, TransactionType.FD_BOOKING)) == 1
    assert _balance(db, alice.account_id) == FUNDS - AMOUNT
    _assert_ledger_consistent(db)


def test_concurrent_bookings_and_transfers_never_overdraw(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    settings: Settings,
    db: Session,
) -> None:
    """Three deposits and three transfers of INR 40,000 race for INR 1 lakh: exactly two fit,
    whichever kinds win, and the account never goes below zero (AGENTS.md section 4 lock order)."""
    alice, bob = make_customer(funds=AMOUNT), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    amount, kinds = 40_000_00, ["book", "transfer"] * 3
    barrier = threading.Barrier(len(kinds))

    def transfer() -> str:
        with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:
            customer = session.get(Customer, alice.customer_id)
            assert customer is not None and alice.account_id is not None
            service = PaymentsService(
                session,
                settings,
                FixedDecisionScorer(FraudDecision.ALLOW),
                auth_service_factory(session),
            )
            command = TransferCommand(
                from_account_id=alice.account_id,
                amount_minor=amount,
                currency="INR",
                payee_id=uuid.UUID(payee["id"]),
                to_account_id=None,
                reference="race",
            )
            barrier.wait()
            try:
                result = service.create_transfer(
                    user_id=alice.user_id,
                    customer=customer,
                    command=command,
                    idempotency_key=uuid.uuid4().hex,
                    meta=RequestMeta(),
                )
            except DomainError as exc:
                session.rollback()
                return exc.code
            return "created" if result.body["status"] == "POSTED" else str(result.body["status"])

    def run(kind: str) -> str:
        if kind == "book":
            key = uuid.uuid4().hex
            return _book_in_thread(migrated_engine, settings, alice, key, amount, barrier)
        return transfer()

    with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
        outcomes = list(pool.map(run, kinds))
    assert outcomes.count("created") == 2, outcomes
    assert sorted(o for o in outcomes if o != "created") == ["insufficient_funds"] * 4
    assert _balance(db, alice.account_id) == AMOUNT - 2 * amount >= 0
    booked = len(_transactions(db, TransactionType.FD_BOOKING))
    assert booked == _count(db, FixedDeposit) and _balance(db, DEPOSITS) == booked * amount
    _assert_ledger_consistent(db)


# ------------------------------------------------------------------ closure (TASK-064)
def test_premature_closure_pays_the_penalty_rate_and_balances(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, clock: FakeClock
) -> None:
    alice = make_customer(funds=FUNDS)
    deposit = _book(client, alice).json()
    clock.advance(140 * DAY)  # one completed quarter and some days into the second
    today = bank_today(datetime.fromtimestamp(clock(), tz=UTC))
    start, maturity = (date.fromisoformat(deposit[k]) for k in ("start_date", "maturity_date"))
    expected = deposit_math.premature_interest(AMOUNT, 710, 100, start, maturity, today)
    at_contract_rate = deposit_math.accrued_interest(AMOUNT, 710, start, maturity, today)
    assert 0 < expected < at_contract_rate

    view = client.get(f"/api/v1/wealth/fixed-deposits/{deposit['id']}", headers=alice.headers)
    assert view.json()["accrued_interest_minor"] == at_contract_rate
    assert view.json()["payout_if_closed_today_minor"] == AMOUNT + expected
    resp = _close(client, alice, deposit["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["status"], body["closure_kind"]) == ("CLOSED", "PREMATURE")
    assert body["paid_interest_minor"] == expected and body["closed_on"] == today.isoformat()

    (principal,) = _transactions(db, TransactionType.FD_CLOSURE)
    (interest,) = _transactions(db, TransactionType.FD_INTEREST)
    assert (principal.debit_account_id, principal.credit_account_id) == (DEPOSITS, alice.account_id)
    assert (interest.debit_account_id, interest.credit_account_id) == (CLEARING, alice.account_id)
    assert (principal.amount, interest.amount) == (AMOUNT, expected)
    assert _balance(db, alice.account_id) == FUNDS + expected
    assert _balance(db, DEPOSITS) == 0 == _open_principal(db)
    _assert_ledger_consistent(db)

    audit = db.execute(
        select(AuditLog).where(AuditLog.action == "FIXED_DEPOSIT_CLOSED")
    ).scalar_one()
    assert audit.details["interest_minor"] == expected
    relay_outbox(db)
    db.commit()
    note = db.execute(
        select(Notification).where(Notification.type == "FIXED_DEPOSIT_CLOSED")
    ).scalar_one()
    assert note.resource_id == principal.id and note.title == "Fixed deposit closed"

    again = _close(client, alice, deposit["id"])
    assert again.status_code == 409 and again.json()["code"] == "deposit_closed"
    assert len(_transactions(db, TransactionType.FD_CLOSURE)) == 1


def test_maturity_is_computed_on_read_and_pays_exactly_the_projected_amount(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, clock: FakeClock
) -> None:
    alice = make_customer(funds=FUNDS)
    deposit = _book(client, alice, FD_SHORT, AMOUNT, 6).json()
    clock.advance(200 * DAY)  # past the 6-month maturity
    view = client.get(f"/api/v1/wealth/fixed-deposits/{deposit['id']}", headers=alice.headers)
    held = view.json()
    assert (held["status"], held["days_to_maturity"]) == ("MATURED", 0)
    assert held["accrued_interest_minor"] == deposit["maturity_interest_minor"] == 3_276_41
    portfolio = client.get("/api/v1/wealth/portfolio", headers=alice.headers).json()
    assert portfolio["accrued_interest_minor"] == deposit["maturity_interest_minor"]

    body = _close(client, alice, deposit["id"]).json()
    assert body["closure_kind"] == "MATURED"
    assert body["paid_interest_minor"] == deposit["maturity_interest_minor"]
    assert _balance(db, alice.account_id) == FUNDS + deposit["maturity_interest_minor"]
    _assert_ledger_consistent(db)


def test_a_locked_in_deposit_cannot_be_closed_early_and_nothing_moves(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, clock: FakeClock
) -> None:
    alice = make_customer(funds=FUNDS)
    deposit = _book(client, alice, FD_TAX_SAVER, 1_00_000_00, 60).json()
    assert deposit["can_close_now"] is False and deposit["payout_if_closed_today_minor"] is None
    clock.advance(400 * DAY)
    resp = _close(client, alice, deposit["id"])
    assert resp.status_code == 422 and resp.json()["code"] == "deposit_locked_in"
    assert _transactions(db, TransactionType.FD_CLOSURE) == []
    assert _balance(db, alice.account_id) == FUNDS - 1_00_000_00


def test_closing_within_the_first_week_returns_the_principal_without_interest(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, clock: FakeClock
) -> None:
    alice = make_customer(funds=FUNDS)
    deposit = _book(client, alice).json()
    clock.advance(3 * DAY)
    body = _close(client, alice, deposit["id"]).json()
    assert body["paid_interest_minor"] == 0 and body["interest_transaction_id"] is None
    assert _transactions(db, TransactionType.FD_INTEREST) == []
    assert _balance(db, alice.account_id) == FUNDS
    _assert_ledger_consistent(db)


def test_concurrent_closures_pay_out_exactly_once(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    settings: Settings,
    db: Session,
) -> None:
    alice = make_customer(funds=FUNDS)
    deposit_id = uuid.UUID(_book(client, alice).json()["id"])
    workers = 5
    barrier = threading.Barrier(workers)

    def close(_: int) -> str:
        assert alice.customer_id is not None
        with Session(migrated_engine, expire_on_commit=False) as session:
            barrier.wait()
            try:
                wealth_service.close_fixed_deposit(
                    session,
                    settings,
                    customer_id=alice.customer_id,
                    user_id=alice.user_id,
                    deposit_id=deposit_id,
                    idempotency_key=uuid.uuid4().hex,
                    meta=RequestMeta(),
                    now=utcnow(),
                )
            except DomainError as exc:
                session.rollback()
                return exc.code
            return "closed"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(close, range(workers)))
    assert sorted(outcomes) == ["closed"] + ["deposit_closed"] * (workers - 1)
    assert len(_transactions(db, TransactionType.FD_CLOSURE)) == 1
    assert _balance(db, alice.account_id) == FUNDS
    _assert_ledger_consistent(db)


def test_closure_retry_with_the_same_key_replays(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, clock: FakeClock
) -> None:
    alice = make_customer(funds=FUNDS)
    deposit = _book(client, alice).json()
    clock.advance(100 * DAY)
    key = uuid.uuid4().hex
    first, retry = (
        _close(client, alice, deposit["id"], key=key),
        _close(client, alice, deposit["id"], key=key),
    )
    assert first.status_code == retry.status_code == 200
    assert retry.headers.get("Idempotent-Replayed") == "true" and retry.json() == first.json()
    assert len(_transactions(db, TransactionType.FD_CLOSURE)) == 1


# ------------------------------------------------------------- portfolio and analytics
def test_portfolio_totals_and_allocation(
    client: TestClient, make_customer: Callable[..., Actor], clock: FakeClock
) -> None:
    alice = make_customer(funds=FUNDS)
    empty = client.get("/api/v1/wealth/portfolio", headers=make_customer().headers).json()
    assert empty["total_minor"] == 0 and {s["share_bps"] for s in empty["allocation"]} == {0}

    _book(client, alice, FD_REGULAR, 50_000_00, 12)
    first = _book(client, alice, FD_SHORT, 30_000_00, 6).json()
    _close(client, alice, first["id"])  # closed deposits are not holdings
    clock.advance(95 * DAY)
    resp = client.get("/api/v1/wealth/portfolio", headers=alice.headers)
    assert resp.status_code == 200
    p = resp.json()
    assert p["cash_minor"] == FUNDS - 50_000_00
    assert p["invested_principal_minor"] == 50_000_00 and p["open_deposits"] == 1
    assert p["accrued_interest_minor"] > 0
    assert p["deposits_value_minor"] == 50_000_00 + p["accrued_interest_minor"]
    assert p["funds_minor"] == 0
    assert p["total_minor"] == p["cash_minor"] + p["deposits_value_minor"]
    shares = {s["asset_class"]: s for s in p["allocation"]}
    assert set(shares) == {"CASH", "DEPOSITS", "FUNDS", "STOCKS", "BONDS"}  # ADR-0058
    assert sum(s["share_bps"] for s in p["allocation"]) == 10_000
    assert shares["DEPOSITS"]["value_minor"] == p["deposits_value_minor"]


def test_a_deposit_is_neither_spending_nor_income_in_analytics(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, clock: FakeClock
) -> None:
    """Booking moves the customer's own money into their deposit (ADR-0025): counted as spend it
    would depress the health score of exactly the customers who save."""
    alice = make_customer(salary=FUNDS // 2)  # recurring income of FUNDS, and a FUNDS balance
    deposit = _book(client, alice).json()
    clock.advance(3 * DAY)
    _close(client, alice, deposit["id"])  # principal back, no interest in the first week
    assert alice.customer_id is not None and alice.account_id is not None
    owned = analytics_repository.owned_account_ids(db, alice.customer_id)
    metrics = analytics_repository.get_financial_health_metrics(
        db, alice.account_id, owned, utcnow()
    )
    assert (metrics.income_minor, metrics.spend_minor) == (FUNDS, 0)
