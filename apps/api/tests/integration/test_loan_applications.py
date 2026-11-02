"""TASK-047 / ADR-0024: loan application, ledger disbursal, schedule and repayments (PostgreSQL)."""

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
from pbip_api.core.errors import DomainError
from pbip_api.modules.accounts.models import Account, AccountStatus
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import (
    EntryDirection,
    Transaction,
    TransactionEntry,
    TransactionStatus,
    TransactionType,
)
from pbip_api.modules.loans import service as loans_service
from pbip_api.modules.loans.catalogue import PRODUCTS, LoanType
from pbip_api.modules.loans.due_dates import due_dates
from pbip_api.modules.loans.models import InstalmentStatus, Loan, LoanInstalment, LoanStatus
from pbip_api.modules.loans.schemas import LoanApplyRequest
from pbip_api.modules.notifications.models import Notification
from pbip_api.modules.notifications.service import relay_outbox
from pbip_api.modules.payments.models import (
    ScheduledTransfer,
    ScheduledTransferStatus,
    ScheduleFrequency,
    SchedulePauseReason,
)
from pbip_api.modules.payments.recurrence import upcoming_occurrences
from pbip_api.modules.payments.scheduled_service import (
    ScheduledExecutionOutcome,
    execute_scheduled_instruction,
)
from pbip_test_support import Actor
from sqlalchemy import Engine, case, func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

PERSONAL = next(p for p in PRODUCTS if p.loan_type == LoanType.PERSONAL)
INCOME = 1_00_000_00  # INR 1 lakh of income this month (make_customer deposits it)
AMOUNT = 1_00_000_00  # INR 1 lakh loan: EMI ~ INR 8,862 over 12 months, well within 50%
CLEARING = ledger.SYSTEM_CLEARING_ID


def _apply(
    client: TestClient,
    actor: Actor,
    amount: int = AMOUNT,
    months: int = 12,
    *,
    key: str | None = None,
    account_id: uuid.UUID | str | None = None,
) -> Any:
    return client.post(
        "/api/v1/loans/apply",
        headers={**actor.headers, "Idempotency-Key": key or uuid.uuid4().hex},
        json={
            "product_id": str(PERSONAL.id),
            "amount_minor": amount,
            "tenure_months": months,
            "account_id": str(account_id or actor.account_id),
        },
    )


def _balance(session: Session, account_id: uuid.UUID | None) -> int:
    account = session.get(Account, account_id, populate_existing=True)
    assert account is not None
    return account.ledger_balance


def _instruction(session: Session, loan_id: uuid.UUID) -> ScheduledTransfer:
    return session.execute(
        select(ScheduledTransfer)
        .where(ScheduledTransfer.loan_id == loan_id)
        .execution_options(populate_existing=True)
    ).scalar_one()


def _instalments(session: Session, loan_id: uuid.UUID) -> list[LoanInstalment]:
    return list(
        session.execute(
            select(LoanInstalment)
            .where(LoanInstalment.loan_id == loan_id)
            .order_by(LoanInstalment.seq)
            .execution_options(populate_existing=True)
        ).scalars()
    )


def _loan_transactions(session: Session, type_: TransactionType) -> list[Transaction]:
    return list(session.execute(select(Transaction).where(Transaction.type == type_)).scalars())


def _assert_every_transaction_balances(session: Session) -> None:
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


def _drain(session: Session, account_id: uuid.UUID, leave: int) -> None:
    """Spend the account down to ``leave`` through the ledger (the only writer of balances)."""
    amount = _balance(session, account_id) - leave
    txn = ledger.create_transaction(
        session,
        type_=TransactionType.TRANSFER,
        amount=amount,
        currency="INR",
        debit_account_id=account_id,
        credit_account_id=CLEARING,
        reference="Test spend",
    )
    ledger.post_transaction(session, txn, ledger.transfer_legs(txn))
    session.commit()


def test_apply_disburses_books_the_schedule_and_links_repayments(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=INCOME)
    resp = _apply(client, alice)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    today = utcnow().date()
    dates = due_dates(today, 12)

    assert body["status"] == "ACTIVE" and body["principal_minor"] == AMOUNT
    assert body["annual_rate_bps"] == PERSONAL.annual_rate_bps  # from the catalogue, not the client
    assert body["outstanding_principal_minor"] == AMOUNT and body["paid_instalments"] == 0
    assert body["next_due_date"] == dates[0].isoformat()
    rows = body["instalments"]
    assert [r["seq"] for r in rows] == list(range(1, 13))
    assert [r["due_date"] for r in rows] == [d.isoformat() for d in dates]
    assert {r["status"] for r in rows} == {"DUE"}
    assert sum(r["principal_minor"] for r in rows) == AMOUNT
    assert body["total_payable_minor"] == AMOUNT + body["total_interest_minor"]

    # Ledger: one LOAN_DISBURSAL, DEBIT clearing / CREDIT the customer, balanced.
    loan_id = uuid.UUID(body["id"])
    assert _balance(db, alice.account_id) == INCOME + AMOUNT
    (disbursal,) = _loan_transactions(db, TransactionType.LOAN_DISBURSAL)
    assert str(disbursal.id) == body["disbursal_transaction_id"]
    assert disbursal.status == TransactionStatus.POSTED
    assert (disbursal.debit_account_id, disbursal.credit_account_id) == (CLEARING, alice.account_id)
    _assert_every_transaction_balances(db)

    # Repayments are one MONTHLY instruction on the scheduled transfers engine.
    instruction = _instruction(db, loan_id)
    assert instruction.frequency == ScheduleFrequency.MONTHLY
    assert instruction.status == ScheduledTransferStatus.ACTIVE
    assert (instruction.start_date, instruction.end_date) == (dates[0], dates[-1])
    assert instruction.next_run_date == dates[0] and instruction.amount == body["emi_minor"]
    assert instruction.from_account_id == alice.account_id and instruction.payee_id is None
    assert alice.customer_id is not None
    projected = upcoming_occurrences(db, alice.customer_id, dates[-1])
    assert [p.occurrence_date for p in projected] == dates  # engine and schedule agree

    # Audit row and a bell notification in the same commit.
    audit = db.execute(select(AuditLog).where(AuditLog.action == "LOAN_DISBURSED")).scalar_one()
    assert audit.resource_id == str(loan_id) and audit.actor_user_id == alice.user_id
    relay_outbox(db)
    db.commit()
    note = db.execute(
        select(Notification).where(
            Notification.user_id == alice.user_id, Notification.type == "LOAN_DISBURSED"
        )
    ).scalar_one()
    assert note.title == "Loan disbursed" and note.resource_id == disbursal.id

    # The active loans list and the detail view.
    listed = client.get("/api/v1/loans", headers=alice.headers).json()
    assert [(item["id"], item["outstanding_principal_minor"]) for item in listed] == [
        (body["id"], AMOUNT)
    ]
    assert listed[0]["next_due_date"] == dates[0].isoformat()
    detail = client.get(f"/api/v1/loans/{loan_id}", headers=alice.headers)
    assert detail.status_code == 200 and detail.json()["instalments"] == rows
    scheduled = client.get("/api/v1/transfers/scheduled", headers=alice.headers).json()
    assert [s["loan_id"] for s in scheduled] == [body["id"]]


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"amount_minor": 0}, 422),
        ({"amount_minor": 20_00_000_00}, 422),  # above the product's INR 15 lakh maximum
        ({"tenure_months": 13}, 422),  # not offered for this product
        ({"customer_id": str(uuid.uuid4())}, 422),  # never accepted from the client
        ({"annual_rate_bps": 1}, 422),  # the rate comes from the catalogue only
        ({"product_id": str(uuid.uuid4())}, 404),
        ({"account_id": "not-a-uuid"}, 422),
    ],
)
def test_apply_rejects_invalid_requests_and_leaves_no_state(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
    change: dict[str, Any],
    status: int,
) -> None:
    alice = make_customer(funds=INCOME)
    payload = {
        "product_id": str(PERSONAL.id),
        "amount_minor": AMOUNT,
        "tenure_months": 12,
        "account_id": str(alice.account_id),
    } | change
    headers = {**alice.headers, "Idempotency-Key": uuid.uuid4().hex}
    resp = client.post("/api/v1/loans/apply", headers=headers, json=payload)
    assert resp.status_code == status, resp.text
    assert db.execute(select(func.count()).select_from(Loan)).scalar_one() == 0
    assert _balance(db, alice.account_id) == INCOME


def test_apply_requires_an_idempotency_key(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=INCOME)
    resp = client.post(
        "/api/v1/loans/apply",
        headers=alice.headers,
        json={
            "product_id": str(PERSONAL.id),
            "amount_minor": AMOUNT,
            "tenure_months": 12,
            "account_id": str(alice.account_id),
        },
    )
    assert resp.status_code == 422


def test_not_eligible_is_refused_with_plain_reasons_and_leaves_no_state(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=50_000_00)  # INR 50,000 income
    resp = _apply(client, alice, 15_00_000_00, 12)  # EMI ~ INR 1.3 lakh
    assert resp.status_code == 422, resp.text
    problem = resp.json()
    assert problem["code"] == "loan_not_eligible"
    assert "above the 50% limit" in problem["title"]
    assert db.execute(select(func.count()).select_from(Loan)).scalar_one() == 0
    assert db.execute(select(func.count()).select_from(ScheduledTransfer)).scalar_one() == 0
    assert _loan_transactions(db, TransactionType.LOAN_DISBURSAL) == []
    assert _balance(db, alice.account_id) == 50_000_00


def test_a_second_loan_counts_the_first_loans_emi(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    """Loan stacking: each loan alone is within 50% of income; together they are not."""
    alice = make_customer(funds=INCOME)
    first = _apply(client, alice, 4_00_000_00, 12)  # EMI ~ INR 35,450: 35% of income
    assert first.status_code == 201, first.text
    eligibility = client.post(
        "/api/v1/loans/check-eligibility",
        headers=alice.headers,
        json={"product_id": str(PERSONAL.id), "amount_minor": 4_00_000_00, "tenure_months": 12},
    ).json()
    assert eligibility["existing_emi_minor"] >= first.json()["emi_minor"]
    second = _apply(client, alice, 4_00_000_00, 12)
    assert second.status_code == 422 and second.json()["code"] == "loan_not_eligible"
    assert db.execute(select(func.count()).select_from(Loan)).scalar_one() == 1


def test_loan_routes_are_customer_only(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Any]
) -> None:
    assert client.get("/api/v1/loans").status_code == 401
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    assert client.get("/api/v1/loans", headers=analyst.headers).status_code == 403
    assert _apply(client, analyst, account_id=uuid.uuid4()).status_code == 403


def test_idempotent_retry_returns_the_same_loan_and_disburses_once(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=INCOME)
    key = uuid.uuid4().hex
    first = _apply(client, alice, key=key)
    retry = _apply(client, alice, key=key)
    assert first.status_code == retry.status_code == 201
    assert retry.headers.get("Idempotent-Replayed") == "true"
    assert retry.json()["id"] == first.json()["id"]
    assert db.execute(select(func.count()).select_from(Loan)).scalar_one() == 1
    assert len(_loan_transactions(db, TransactionType.LOAN_DISBURSAL)) == 1
    assert _balance(db, alice.account_id) == INCOME + AMOUNT
    reused = _apply(client, alice, AMOUNT + 5_000_00, key=key)  # same key, different request
    assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reuse"


def _apply_in_thread(
    engine: Engine, settings: Settings, actor: Actor, key: str, barrier: threading.Barrier
) -> str:
    assert actor.customer_id is not None and actor.account_id is not None
    with Session(engine, expire_on_commit=False) as session:
        barrier.wait()
        try:
            result = loans_service.apply_for_loan(
                session,
                settings,
                customer_id=actor.customer_id,
                user_id=actor.user_id,
                request=LoanApplyRequest(
                    product_id=PERSONAL.id,
                    amount_minor=4_00_000_00,
                    tenure_months=12,
                    account_id=actor.account_id,
                ),
                idempotency_key=key,
                meta=RequestMeta(),
            )
        except DomainError as exc:
            session.rollback()
            return exc.code
        return "replayed" if result.replayed else "created"


def test_concurrent_duplicate_applies_create_exactly_one_loan_and_one_disbursal(
    make_customer: Callable[..., Actor], migrated_engine: Engine, settings: Settings, db: Session
) -> None:
    """Real threads, real PostgreSQL: a double-clicked submit sent six times at once."""
    alice = make_customer(funds=INCOME)
    workers, key = 6, uuid.uuid4().hex
    barrier = threading.Barrier(workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(
            pool.map(
                lambda _: _apply_in_thread(migrated_engine, settings, alice, key, barrier),
                range(workers),
            )
        )
    assert sorted(outcomes) == ["created"] + ["replayed"] * (workers - 1)
    assert db.execute(select(func.count()).select_from(Loan)).scalar_one() == 1
    assert len(_loan_transactions(db, TransactionType.LOAN_DISBURSAL)) == 1
    assert _balance(db, alice.account_id) == INCOME + 4_00_000_00
    assert _balance(db, CLEARING) == -(INCOME + 4_00_000_00)
    _assert_every_transaction_balances(db)


def test_concurrent_applications_with_different_keys_cannot_stack_loans(
    make_customer: Callable[..., Actor], migrated_engine: Engine, settings: Settings, db: Session
) -> None:
    """Each application alone fits the 50% limit; the per-user lock serialises them, so the
    later ones see the first loan's EMI and are refused."""
    alice = make_customer(funds=INCOME)
    workers = 4
    barrier = threading.Barrier(workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(
            pool.map(
                lambda _: _apply_in_thread(
                    migrated_engine, settings, alice, uuid.uuid4().hex, barrier
                ),
                range(workers),
            )
        )
    assert sorted(outcomes) == ["created"] + ["loan_not_eligible"] * (workers - 1)
    assert db.execute(select(func.count()).select_from(Loan)).scalar_one() == 1
    assert len(_loan_transactions(db, TransactionType.LOAN_DISBURSAL)) == 1


# ------------------------------------------------------------------ repayments
@pytest.fixture
def loan(
    client: TestClient, make_customer: Callable[..., Actor]
) -> tuple[Actor, uuid.UUID, list[date]]:
    alice = make_customer(funds=INCOME)
    resp = _apply(client, alice)
    assert resp.status_code == 201, resp.text
    return alice, uuid.UUID(resp.json()["id"]), due_dates(utcnow().date(), 12)


def _run(engine: Engine, instruction_id: uuid.UUID, on: date) -> ScheduledExecutionOutcome:
    with Session(engine, expire_on_commit=False) as session:
        return execute_scheduled_instruction(session, instruction_id, today_utc=on)


def test_a_repayment_marks_the_right_instalment_paid_and_reduces_the_balance(
    client: TestClient,
    migrated_engine: Engine,
    db: Session,
    loan: tuple[Actor, uuid.UUID, list[date]],
) -> None:
    alice, loan_id, dates = loan
    instruction = _instruction(db, loan_id)
    assert _run(migrated_engine, instruction.id, dates[0] - timedelta(days=1)) == (
        ScheduledExecutionOutcome.SKIPPED_INACTIVE  # not due yet
    )
    assert _run(migrated_engine, instruction.id, dates[0]) == ScheduledExecutionOutcome.POSTED

    rows = _instalments(db, loan_id)
    assert [r.status for r in rows] == [InstalmentStatus.PAID] + [InstalmentStatus.DUE] * 11
    (repayment,) = _loan_transactions(db, TransactionType.LOAN_REPAYMENT)
    assert rows[0].transaction_id == repayment.id and rows[0].paid_at is not None
    assert repayment.amount == rows[0].amount
    assert (repayment.debit_account_id, repayment.credit_account_id) == (alice.account_id, CLEARING)
    assert _balance(db, alice.account_id) == INCOME + AMOUNT - rows[0].amount
    _assert_every_transaction_balances(db)
    instruction = _instruction(db, loan_id)
    assert instruction.next_run_date == dates[1]
    assert instruction.last_transaction_id == repayment.id

    detail = client.get(f"/api/v1/loans/{loan_id}", headers=alice.headers).json()
    assert detail["paid_instalments"] == 1
    assert detail["outstanding_principal_minor"] == AMOUNT - rows[0].principal
    assert detail["outstanding_principal_minor"] == rows[0].balance_after
    assert detail["next_due_date"] == dates[1].isoformat()
    assert detail["instalments"][0]["status"] == "PAID"

    # A redelivered task the same day finds nothing due and posts nothing.
    assert (
        _run(migrated_engine, instruction.id, dates[0])
        == ScheduledExecutionOutcome.SKIPPED_INACTIVE
    )
    assert len(_loan_transactions(db, TransactionType.LOAN_REPAYMENT)) == 1


def test_concurrent_repayment_runs_post_the_instalment_exactly_once(
    migrated_engine: Engine, db: Session, loan: tuple[Actor, uuid.UUID, list[date]]
) -> None:
    alice, loan_id, dates = loan
    instruction_id = _instruction(db, loan_id).id
    workers = 5
    barrier = threading.Barrier(workers)

    def run(_: int) -> ScheduledExecutionOutcome:
        with Session(migrated_engine, expire_on_commit=False) as session:
            barrier.wait()
            return execute_scheduled_instruction(session, instruction_id, today_utc=dates[0])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(run, range(workers)))
    assert outcomes.count(ScheduledExecutionOutcome.POSTED) == 1, outcomes
    assert len(_loan_transactions(db, TransactionType.LOAN_REPAYMENT)) == 1
    assert sum(r.status == InstalmentStatus.PAID for r in _instalments(db, loan_id)) == 1
    _assert_every_transaction_balances(db)


def test_a_missed_repayment_is_retried_and_arrears_are_collected_together(
    migrated_engine: Engine,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    loan: tuple[Actor, uuid.UUID, list[date]],
) -> None:
    alice, loan_id, dates = loan
    assert alice.account_id is not None and alice.customer_id is not None
    instruction_id = _instruction(db, loan_id).id
    _drain(db, alice.account_id, leave=100)

    assert _run(migrated_engine, instruction_id, dates[0]) == (
        ScheduledExecutionOutcome.RETRYABLE_FAILURE
    )
    # The next day the arrears are retried, NOT skipped like a missed standing order.
    assert _run(migrated_engine, instruction_id, dates[0] + timedelta(days=1)) == (
        ScheduledExecutionOutcome.RETRYABLE_FAILURE
    )
    instruction = _instruction(db, loan_id)
    assert instruction.status == ScheduledTransferStatus.ACTIVE
    assert instruction.next_run_date == dates[0] and instruction.failure_count == 2
    assert _loan_transactions(db, TransactionType.LOAN_REPAYMENT) == []

    late = datetime.combine(dates[0] + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    monkeypatch.setattr(loans_service, "utcnow", lambda: late)
    overdue = loans_service.get_loan(db, alice.customer_id, loan_id)
    assert overdue.instalments[0].status == "OVERDUE" and overdue.overdue_instalments == 1

    ledger.deposit(db, alice.account_id, 50_000_00)
    db.commit()
    assert _run(migrated_engine, instruction_id, dates[1]) == ScheduledExecutionOutcome.POSTED
    rows = _instalments(db, loan_id)
    (repayment,) = _loan_transactions(db, TransactionType.LOAN_REPAYMENT)
    assert repayment.amount == rows[0].amount + rows[1].amount  # arrears + current, one posting
    assert [r.transaction_id for r in rows[:2]] == [repayment.id, repayment.id]
    assert all(r.status == InstalmentStatus.DUE for r in rows[2:])
    assert _instruction(db, loan_id).next_run_date == dates[2]


def test_the_last_repayment_closes_the_loan_and_completes_the_instruction(
    client: TestClient,
    migrated_engine: Engine,
    db: Session,
    loan: tuple[Actor, uuid.UUID, list[date]],
) -> None:
    alice, loan_id, dates = loan
    instruction_id = _instruction(db, loan_id).id
    assert _run(migrated_engine, instruction_id, dates[-1]) == ScheduledExecutionOutcome.POSTED
    closed = db.get(Loan, loan_id, populate_existing=True)
    assert closed is not None and closed.status == LoanStatus.CLOSED
    assert closed.closed_at is not None
    assert _instruction(db, loan_id).status == ScheduledTransferStatus.COMPLETED
    (repayment,) = _loan_transactions(db, TransactionType.LOAN_REPAYMENT)
    assert repayment.amount == closed.principal + closed.total_interest
    assert _balance(db, alice.account_id) == INCOME + AMOUNT - repayment.amount
    assert client.get("/api/v1/loans", headers=alice.headers).json() == []
    detail = client.get(f"/api/v1/loans/{loan_id}", headers=alice.headers).json()
    assert detail["status"] == "CLOSED" and detail["outstanding_principal_minor"] == 0
    assert detail["next_due_date"] is None
    _assert_every_transaction_balances(db)
    for account_id in (alice.account_id, CLEARING):
        assert account_id is not None
        assert ledger.entry_balance(db, account_id) == _balance(db, account_id)


def test_a_frozen_loan_account_pauses_repayments_until_resumed(
    client: TestClient,
    migrated_engine: Engine,
    db: Session,
    loan: tuple[Actor, uuid.UUID, list[date]],
) -> None:
    alice, loan_id, dates = loan
    instruction_id = _instruction(db, loan_id).id
    account = db.get(Account, alice.account_id)
    assert account is not None
    account.status = AccountStatus.FROZEN
    db.commit()
    assert _run(migrated_engine, instruction_id, dates[0]) == (
        ScheduledExecutionOutcome.PERMANENT_FAILURE
    )
    paused = _instruction(db, loan_id)
    assert paused.status == ScheduledTransferStatus.PAUSED
    assert paused.pause_reason == SchedulePauseReason.CUSTOMER_ACTION_REQUIRED
    account.status = AccountStatus.ACTIVE
    db.commit()
    resumed = client.post(
        f"/api/v1/transfers/scheduled/{instruction_id}/resume", headers=alice.headers
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "ACTIVE"
    assert resumed.json()["next_run_date"] == dates[0].isoformat()  # arrears are not skipped


def test_customers_cannot_pause_or_cancel_a_loan_repayment_instruction(
    client: TestClient, db: Session, loan: tuple[Actor, uuid.UUID, list[date]]
) -> None:
    alice, loan_id, _ = loan
    instruction_id = _instruction(db, loan_id).id
    base = f"/api/v1/transfers/scheduled/{instruction_id}"
    assert client.post(f"{base}/pause", headers=alice.headers).status_code == 409
    assert client.post(f"{base}/cancel", headers=alice.headers).status_code == 409
    assert client.delete(base, headers=alice.headers).status_code == 409
    assert _instruction(db, loan_id).status == ScheduledTransferStatus.ACTIVE
