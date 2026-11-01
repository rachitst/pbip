"""PostgreSQL integration tests for scheduled transfers API endpoints (TASK-015 / ADR-0021)."""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.modules.accounts.models import Account, AccountType
from pbip_api.modules.accounts.service import open_account
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.ledger.models import Transaction, TransactionType
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
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

AMOUNT = 15_000


@pytest.fixture
def pair(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> tuple[Actor, Actor, dict[str, Any]]:
    alice = make_customer(funds=1_000_000)
    bob = make_customer(funds=0)
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    return alice, bob, payee


# =============================================================================
# A. Creation Tests
# =============================================================================


def test_create_scheduled_transfer_success_payee(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice, _, payee = pair
    start = (utcnow() + timedelta(days=1)).date()
    end = start + timedelta(days=30)

    res = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "reference": "Monthly rent",
            "frequency": "DAILY",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["customer_id"] == str(alice.customer_id)
    assert data["from_account_id"] == str(alice.account_id)
    assert data["payee_id"] == payee["id"]
    assert data["to_account_id"] is None
    assert data["amount_minor"] == AMOUNT
    assert data["currency"] == "INR"
    assert data["frequency"] == "DAILY"
    assert data["status"] == "ACTIVE"
    assert data["start_date"] == start.isoformat()
    assert data["end_date"] == end.isoformat()
    assert data["next_run_date"] == start.isoformat()
    assert data["pause_reason"] is None
    assert data["failure_count"] == 0

    # Verify audit record
    with Session(migrated_engine) as session:
        audit = session.execute(
            select(AuditLog).where(
                AuditLog.action == "SCHEDULED_TRANSFER_CREATED",
                AuditLog.resource_id == data["id"],
            )
        ).scalar_one_or_none()
        assert audit is not None
        assert audit.actor_user_id == alice.user_id


def test_create_scheduled_transfer_success_own_account(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    db: Session,
) -> None:
    alice, _, _ = pair
    second_acc = open_account(db, alice.customer_id, AccountType.SAVINGS, commit=True)
    start = (utcnow() + timedelta(days=2)).date()

    res = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 20_000,
            "currency": "INR",
            "to_account_id": str(second_acc.id),
            "reference": "Savings transfer",
            "frequency": "MONTHLY",
            "start_date": start.isoformat(),
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["to_account_id"] == str(second_acc.id)
    assert data["payee_id"] is None
    assert data["frequency"] == "MONTHLY"
    assert data["end_date"] is None


def test_create_scheduled_transfer_does_not_post_ledger_transaction(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    db: Session,
) -> None:
    alice, _, payee = pair
    start = (utcnow() + timedelta(days=1)).date()

    res = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "frequency": "DAILY",
            "start_date": start.isoformat(),
        },
    )
    assert res.status_code == 201

    # Check that no ledger transactions of type TRANSFER were posted
    txs = (
        db.execute(select(Transaction).where(Transaction.type == TransactionType.TRANSFER))
        .scalars()
        .all()
    )
    assert len(txs) == 0

    # Alice balance untouched
    acc = db.get(Account, alice.account_id)
    assert acc is not None and acc.ledger_balance == 1_000_000


def test_create_scheduled_transfer_invalid_inputs(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _, payee = pair
    tomorrow = (utcnow() + timedelta(days=1)).date()
    yesterday = (utcnow() - timedelta(days=1)).date()

    # 1. Invalid frequency
    res = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "frequency": "HOURLY",
            "start_date": tomorrow.isoformat(),
        },
    )
    assert res.status_code == 422

    # 2. Start date in past
    res = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "frequency": "DAILY",
            "start_date": yesterday.isoformat(),
        },
    )
    assert res.status_code == 422

    # 3. End date before start date
    res = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "frequency": "DAILY",
            "start_date": tomorrow.isoformat(),
            "end_date": yesterday.isoformat(),
        },
    )
    assert res.status_code == 422

    # 4. Amount <= 0
    res = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 0,
            "currency": "INR",
            "payee_id": payee["id"],
            "frequency": "DAILY",
            "start_date": tomorrow.isoformat(),
        },
    )
    assert res.status_code == 422

    # 5. Missing both payee_id and to_account_id
    res = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "frequency": "DAILY",
            "start_date": tomorrow.isoformat(),
        },
    )
    assert res.status_code == 422

    # 6. Both payee_id and to_account_id supplied
    res = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "to_account_id": str(alice.account_id),
            "frequency": "DAILY",
            "start_date": tomorrow.isoformat(),
        },
    )
    assert res.status_code == 422


def test_create_scheduled_transfer_unauthorized_source_account(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    make_customer: Callable[..., Actor],
) -> None:
    alice, _, payee = pair
    mallory = make_customer()
    tomorrow = (utcnow() + timedelta(days=1)).date()

    # Mallory attempts to create schedule using Alice's account
    res = client.post(
        "/api/v1/transfers/scheduled",
        headers=mallory.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "frequency": "DAILY",
            "start_date": tomorrow.isoformat(),
        },
    )
    assert res.status_code == 404


# =============================================================================
# B. Listing and Retrieval Tests
# =============================================================================


def test_list_and_get_scheduled_transfers_with_bola_isolation(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice, _, alice_payee = pair
    bob = make_customer(funds=100_000)
    charlie = make_customer()
    bob_payee = make_verified_payee(bob, charlie)
    age_payee(migrated_engine, bob_payee["id"], 48)

    tomorrow = (utcnow() + timedelta(days=1)).date()

    # Alice creates 2 schedules
    r1 = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 10_000,
            "currency": "INR",
            "payee_id": alice_payee["id"],
            "frequency": "DAILY",
            "start_date": tomorrow.isoformat(),
        },
    ).json()
    r2 = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 20_000,
            "currency": "INR",
            "payee_id": alice_payee["id"],
            "frequency": "WEEKLY",
            "start_date": tomorrow.isoformat(),
        },
    ).json()

    # Bob creates 1 schedule
    r3 = client.post(
        "/api/v1/transfers/scheduled",
        headers=bob.headers,
        json={
            "from_account_id": str(bob.account_id),
            "amount_minor": 30_000,
            "currency": "INR",
            "payee_id": bob_payee["id"],
            "frequency": "MONTHLY",
            "start_date": tomorrow.isoformat(),
        },
    ).json()

    # Alice lists her schedules
    alice_list = client.get("/api/v1/transfers/scheduled", headers=alice.headers).json()
    assert len(alice_list) == 2
    alice_ids = {item["id"] for item in alice_list}
    assert alice_ids == {r1["id"], r2["id"]}
    assert r3["id"] not in alice_ids

    # Bob lists his schedules
    bob_list = client.get("/api/v1/transfers/scheduled", headers=bob.headers).json()
    assert len(bob_list) == 1
    assert bob_list[0]["id"] == r3["id"]

    # Alice retrieves her own schedule
    res = client.get(f"/api/v1/transfers/scheduled/{r1['id']}", headers=alice.headers)
    assert res.status_code == 200
    assert res.json()["id"] == r1["id"]

    # Bob attempts to retrieve Alice's schedule (BOLA -> 404)
    res_bola = client.get(f"/api/v1/transfers/scheduled/{r1['id']}", headers=bob.headers)
    assert res_bola.status_code == 404

    # Non-existent ID returns 404
    res_404 = client.get(f"/api/v1/transfers/scheduled/{uuid.uuid4()}", headers=alice.headers)
    assert res_404.status_code == 404


def test_list_scheduled_transfers_pagination_and_status_filter(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _, payee = pair
    tomorrow = (utcnow() + timedelta(days=1)).date()

    for i in range(5):
        client.post(
            "/api/v1/transfers/scheduled",
            headers=alice.headers,
            json={
                "from_account_id": str(alice.account_id),
                "amount_minor": 10_000 + i,
                "currency": "INR",
                "payee_id": payee["id"],
                "frequency": "DAILY",
                "start_date": tomorrow.isoformat(),
            },
        )

    # Limit and offset
    page1 = client.get("/api/v1/transfers/scheduled?limit=2&offset=0", headers=alice.headers).json()
    page2 = client.get("/api/v1/transfers/scheduled?limit=2&offset=2", headers=alice.headers).json()
    assert len(page1) == 2
    assert len(page2) == 2
    assert page1[0]["id"] != page2[0]["id"]

    # Filter by status
    active = client.get("/api/v1/transfers/scheduled?status=ACTIVE", headers=alice.headers).json()
    assert len(active) == 5
    paused = client.get("/api/v1/transfers/scheduled?status=PAUSED", headers=alice.headers).json()
    assert len(paused) == 0


# =============================================================================
# C. Cancellation Tests
# =============================================================================


def test_cancel_scheduled_transfer_endpoints(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice, _, payee = pair
    tomorrow = (utcnow() + timedelta(days=1)).date()

    # 1. Cancel via POST /cancel
    st1 = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "frequency": "DAILY",
            "start_date": tomorrow.isoformat(),
        },
    ).json()

    res = client.post(f"/api/v1/transfers/scheduled/{st1['id']}/cancel", headers=alice.headers)
    assert res.status_code == 200
    assert res.json()["status"] == "CANCELLED"

    # Verify audit
    with Session(migrated_engine) as session:
        audit = session.execute(
            select(AuditLog).where(
                AuditLog.action == "SCHEDULED_TRANSFER_CANCELLED",
                AuditLog.resource_id == st1["id"],
            )
        ).scalar_one_or_none()
        assert audit is not None

    # Repeated cancellation returns 409 Conflict
    res_repeat = client.post(
        f"/api/v1/transfers/scheduled/{st1['id']}/cancel", headers=alice.headers
    )
    assert res_repeat.status_code == 409

    # 2. Cancel via DELETE alias
    st2 = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "frequency": "DAILY",
            "start_date": tomorrow.isoformat(),
        },
    ).json()

    res_del = client.delete(f"/api/v1/transfers/scheduled/{st2['id']}", headers=alice.headers)
    assert res_del.status_code == 200
    assert res_del.json()["status"] == "CANCELLED"


def test_cancel_completed_instruction_returns_409(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice, _, payee = pair
    tomorrow = (utcnow() + timedelta(days=1)).date()

    st = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "frequency": "DAILY",
            "start_date": tomorrow.isoformat(),
        },
    ).json()

    # Manually set to COMPLETED in database
    with Session(migrated_engine) as session:
        row = session.get(ScheduledTransfer, uuid.UUID(st["id"]))
        assert row is not None
        row.status = ScheduledTransferStatus.COMPLETED
        session.commit()

    res = client.post(f"/api/v1/transfers/scheduled/{st['id']}/cancel", headers=alice.headers)
    assert res.status_code == 409


# =============================================================================
# D. Pause and Resume Tests
# =============================================================================


def test_pause_and_resume_lifecycle(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice, _, payee = pair
    tomorrow = (utcnow() + timedelta(days=1)).date()

    st = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "frequency": "DAILY",
            "start_date": tomorrow.isoformat(),
        },
    ).json()

    # 1. Pause active
    res_pause = client.post(f"/api/v1/transfers/scheduled/{st['id']}/pause", headers=alice.headers)
    assert res_pause.status_code == 200
    data_pause = res_pause.json()
    assert data_pause["status"] == "PAUSED"
    assert data_pause["pause_reason"] == "CUSTOMER"

    # Pausing again returns 409
    res_pause_again = client.post(
        f"/api/v1/transfers/scheduled/{st['id']}/pause", headers=alice.headers
    )
    assert res_pause_again.status_code == 409

    # 2. Resume paused
    res_resume = client.post(
        f"/api/v1/transfers/scheduled/{st['id']}/resume", headers=alice.headers
    )
    assert res_resume.status_code == 200
    data_resume = res_resume.json()
    assert data_resume["status"] == "ACTIVE"
    assert data_resume["pause_reason"] is None

    # Resuming active returns 409
    res_resume_again = client.post(
        f"/api/v1/transfers/scheduled/{st['id']}/resume", headers=alice.headers
    )
    assert res_resume_again.status_code == 409


def test_resume_fraud_block_fails_with_409(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice, _, payee = pair
    tomorrow = (utcnow() + timedelta(days=1)).date()

    st = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "frequency": "DAILY",
            "start_date": tomorrow.isoformat(),
        },
    ).json()

    # Set pause_reason to FRAUD_BLOCK in database
    with Session(migrated_engine) as session:
        row = session.get(ScheduledTransfer, uuid.UUID(st["id"]))
        assert row is not None
        row.status = ScheduledTransferStatus.PAUSED
        row.pause_reason = SchedulePauseReason.FRAUD_BLOCK
        session.commit()

    # Customer resume fails with 409
    res = client.post(f"/api/v1/transfers/scheduled/{st['id']}/resume", headers=alice.headers)
    assert res.status_code == 409
    assert "requires fraud analyst clearance" in res.json()["title"]


def test_resume_roll_forward_past_due_date(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    """When resuming an instruction whose next_run_date is in the past, it rolls forward without catch-up."""
    alice, _, payee = pair
    past_date = date(2026, 10, 1)

    st = client.post(
        "/api/v1/transfers/scheduled",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": AMOUNT,
            "currency": "INR",
            "payee_id": payee["id"],
            "frequency": "DAILY",
            "start_date": (utcnow() + timedelta(days=1)).date().isoformat(),
        },
    ).json()

    today = utcnow().date()

    # Backdate start_date and next_run_date into the past while PAUSED
    with Session(migrated_engine) as session:
        row = session.get(ScheduledTransfer, uuid.UUID(st["id"]))
        assert row is not None
        row.status = ScheduledTransferStatus.PAUSED
        row.pause_reason = SchedulePauseReason.CUSTOMER
        row.start_date = past_date
        row.next_run_date = past_date
        session.commit()

    res = client.post(f"/api/v1/transfers/scheduled/{st['id']}/resume", headers=alice.headers)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ACTIVE"
    # Rolled forward to at least today
    assert date.fromisoformat(data["next_run_date"]) >= today


# =============================================================================
# E. Security Tests
# =============================================================================


def test_unauthenticated_requests_are_rejected(
    client: TestClient,
) -> None:
    res = client.get("/api/v1/transfers/scheduled")
    assert res.status_code == 401

    res = client.post("/api/v1/transfers/scheduled", json={})
    assert res.status_code == 401

    fake_id = uuid.uuid4()
    assert client.get(f"/api/v1/transfers/scheduled/{fake_id}").status_code == 401
    assert client.post(f"/api/v1/transfers/scheduled/{fake_id}/cancel").status_code == 401
    assert client.post(f"/api/v1/transfers/scheduled/{fake_id}/pause").status_code == 401
    assert client.post(f"/api/v1/transfers/scheduled/{fake_id}/resume").status_code == 401


def test_staff_without_customer_profile_returns_403(
    client: TestClient,
    make_staff: Callable[..., Actor],
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    assert client.get("/api/v1/transfers/scheduled", headers=analyst.headers).status_code == 403
    assert (
        client.post("/api/v1/transfers/scheduled", headers=analyst.headers, json={}).status_code
        == 403
    )


# =============================================================================
# F. Concurrency Tests
# =============================================================================


def test_cancel_racing_with_worker_execution(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    """Proves that customer cancel and worker execution serialise cleanly without deadlock."""
    alice, _, payee = pair
    today = date(2026, 11, 1)

    with Session(migrated_engine) as session:
        inst = ScheduledTransfer(
            customer_id=alice.customer_id,
            user_id=alice.user_id,
            from_account_id=alice.account_id,
            payee_id=uuid.UUID(str(payee["id"])),
            amount=AMOUNT,
            currency="INR",
            frequency=ScheduleFrequency.DAILY,
            status=ScheduledTransferStatus.ACTIVE,
            start_date=today,
            next_run_date=today,
        )
        session.add(inst)
        session.commit()
        inst_id = inst.id

    def run_worker() -> ScheduledExecutionOutcome:
        with Session(migrated_engine) as worker_session:
            return execute_scheduled_instruction(worker_session, inst_id, today_utc=today)

    def run_cancel() -> int:
        res = client.post(f"/api/v1/transfers/scheduled/{inst_id}/cancel", headers=alice.headers)
        return res.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_worker = pool.submit(run_worker)
        f_cancel = pool.submit(run_cancel)
        worker_res = f_worker.result()
        cancel_res = f_cancel.result()

    # Either worker posted first and cancel saw POSTED/ACTIVE and cancelled next,
    # or cancel ran first and worker saw CANCELLED and skipped.
    assert cancel_res in (200, 409)
    assert worker_res in (
        ScheduledExecutionOutcome.POSTED,
        ScheduledExecutionOutcome.SKIPPED_INACTIVE,
    )

    with Session(migrated_engine) as session:
        final_inst = session.get(ScheduledTransfer, inst_id)
        assert final_inst is not None
        # In all races, no corrupt state is created
        assert final_inst.status in (
            ScheduledTransferStatus.CANCELLED,
            ScheduledTransferStatus.ACTIVE,
        )


def test_pause_racing_with_worker_execution(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    """Proves customer pause and worker execution serialise cleanly without deadlock."""
    alice, _, payee = pair
    today = date(2026, 11, 1)

    with Session(migrated_engine) as session:
        inst = ScheduledTransfer(
            customer_id=alice.customer_id,
            user_id=alice.user_id,
            from_account_id=alice.account_id,
            payee_id=uuid.UUID(str(payee["id"])),
            amount=AMOUNT,
            currency="INR",
            frequency=ScheduleFrequency.DAILY,
            status=ScheduledTransferStatus.ACTIVE,
            start_date=today,
            next_run_date=today,
        )
        session.add(inst)
        session.commit()
        inst_id = inst.id

    barrier = threading.Barrier(2)

    def run_worker() -> ScheduledExecutionOutcome:
        barrier.wait(timeout=10)
        with Session(migrated_engine) as worker_session:
            return execute_scheduled_instruction(worker_session, inst_id, today_utc=today)

    def run_pause() -> int:
        barrier.wait(timeout=10)
        res = client.post(f"/api/v1/transfers/scheduled/{inst_id}/pause", headers=alice.headers)
        return res.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_worker = pool.submit(run_worker)
        f_pause = pool.submit(run_pause)
        worker_res = f_worker.result()
        pause_res = f_pause.result()

    # Pause must succeed (200 OK)
    assert pause_res == 200
    assert worker_res in (
        ScheduledExecutionOutcome.POSTED,
        ScheduledExecutionOutcome.SKIPPED_INACTIVE,
    )

    with Session(migrated_engine) as session:
        final_inst = session.get(ScheduledTransfer, inst_id)
        assert final_inst is not None
        assert final_inst.status == ScheduledTransferStatus.PAUSED
        assert final_inst.pause_reason == SchedulePauseReason.CUSTOMER

        # Verify ledger transfer count matches winner
        txs = (
            session.execute(select(Transaction).where(Transaction.type == TransactionType.TRANSFER))
            .scalars()
            .all()
        )
        if worker_res == ScheduledExecutionOutcome.POSTED:
            assert len(txs) == 1
            assert final_inst.next_run_date == today + timedelta(days=1)
            assert final_inst.last_run_date == today
        else:
            assert len(txs) == 0
            assert final_inst.next_run_date == today
            assert final_inst.last_run_date is None

    # Subsequent worker poll must skip execution because instruction remains PAUSED
    with Session(migrated_engine) as session:
        subsequent_outcome = execute_scheduled_instruction(session, inst_id, today_utc=today)
        assert subsequent_outcome == ScheduledExecutionOutcome.SKIPPED_INACTIVE

        # Verify no duplicate transactions were created
        txs_after = (
            session.execute(select(Transaction).where(Transaction.type == TransactionType.TRANSFER))
            .scalars()
            .all()
        )
        assert len(txs_after) == (1 if worker_res == ScheduledExecutionOutcome.POSTED else 0)


def test_resume_racing_with_worker_execution(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    """Proves customer resume and worker execution serialise cleanly without deadlock."""
    alice, _, payee = pair
    today = date(2026, 11, 1)

    with Session(migrated_engine) as session:
        inst = ScheduledTransfer(
            customer_id=alice.customer_id,
            user_id=alice.user_id,
            from_account_id=alice.account_id,
            payee_id=uuid.UUID(str(payee["id"])),
            amount=AMOUNT,
            currency="INR",
            frequency=ScheduleFrequency.DAILY,
            status=ScheduledTransferStatus.PAUSED,
            pause_reason=SchedulePauseReason.CUSTOMER,
            start_date=today,
            next_run_date=today,
        )
        session.add(inst)
        session.commit()
        inst_id = inst.id

    barrier = threading.Barrier(2)

    def run_worker() -> ScheduledExecutionOutcome:
        barrier.wait(timeout=10)
        with Session(migrated_engine) as worker_session:
            return execute_scheduled_instruction(worker_session, inst_id, today_utc=today)

    def run_resume() -> int:
        barrier.wait(timeout=10)
        res = client.post(f"/api/v1/transfers/scheduled/{inst_id}/resume", headers=alice.headers)
        return res.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_worker = pool.submit(run_worker)
        f_resume = pool.submit(run_resume)
        worker_res = f_worker.result()
        resume_res = f_resume.result()

    # Resume must succeed (200 OK)
    assert resume_res == 200
    assert worker_res in (
        ScheduledExecutionOutcome.POSTED,
        ScheduledExecutionOutcome.SKIPPED_INACTIVE,
    )

    with Session(migrated_engine) as session:
        final_inst = session.get(ScheduledTransfer, inst_id)
        assert final_inst is not None
        assert final_inst.status == ScheduledTransferStatus.ACTIVE
        assert final_inst.pause_reason is None

        txs = (
            session.execute(select(Transaction).where(Transaction.type == TransactionType.TRANSFER))
            .scalars()
            .all()
        )

        if worker_res == ScheduledExecutionOutcome.POSTED:
            # Worker executed after resume activated the instruction
            assert len(txs) == 1
            assert final_inst.next_run_date == today + timedelta(days=1)
            assert final_inst.last_run_date == today
        else:
            # Worker checked before resume activated the instruction, saw PAUSED, skipped
            assert len(txs) == 0
            assert final_inst.next_run_date == today
            assert final_inst.last_run_date is None

    # Verify a subsequent worker poll behaves correctly for the resulting state:
    with Session(migrated_engine) as session:
        subsequent_outcome = execute_scheduled_instruction(session, inst_id, today_utc=today)
        if worker_res == ScheduledExecutionOutcome.POSTED:
            # Already posted for today; next_run_date is tomorrow, so it skips
            assert subsequent_outcome == ScheduledExecutionOutcome.SKIPPED_INACTIVE
        else:
            # Resumed instruction is now due today and ready to execute
            assert subsequent_outcome == ScheduledExecutionOutcome.POSTED

        # In all cases, exactly 1 transfer is posted overall (no duplicate posting)
        txs_after = (
            session.execute(select(Transaction).where(Transaction.type == TransactionType.TRANSFER))
            .scalars()
            .all()
        )
        assert len(txs_after) == 1
