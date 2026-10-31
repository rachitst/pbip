"""Integration tests for utility billers and bill payments against real PostgreSQL."""

import json
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.metrics import IDEMPOTENT_REPLAYS_TOTAL
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.bills.models import Biller, BillerCategory, BillerStatus, BillPayment
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import (
    EntryDirection,
    Transaction,
    TransactionEntry,
    TransactionStatus,
    TransactionType,
)
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.notifications.models import Notification, OutboxEvent
from pbip_test_support import Actor
from sqlalchemy import Engine, case, func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

MSEB_BILLER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
WATER_BILLER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def new_key() -> dict[str, str]:
    return {"Idempotency-Key": f"bill-{uuid.uuid4().hex}"}


def test_list_billers_returns_seeded_active_billers_and_filters_inactive(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    resp = client.get("/api/v1/bills/billers", headers=alice.headers)
    assert resp.status_code == 200, resp.text
    billers = resp.json()
    assert len(billers) >= 5
    codes = {b["biller_code"] for b in billers}
    assert {"MSEB-MH", "BMC-WTR", "JIO-FBR", "MGL-GAS", "AIRTEL-POST"} <= codes
    assert all(b["status"] == "ACTIVE" for b in billers)

    inactive = Biller(
        id=uuid.uuid4(),
        name="Legacy Telegraph Co",
        category=BillerCategory.TELECOM,
        biller_code="OLD-TEL",
        status=BillerStatus.INACTIVE,
    )
    db.add(inactive)
    db.commit()

    active_only = client.get("/api/v1/bills/billers", headers=alice.headers).json()
    assert "OLD-TEL" not in {b["biller_code"] for b in active_only}

    all_billers = client.get(
        "/api/v1/bills/billers?active_only=false", headers=alice.headers
    ).json()
    assert "OLD-TEL" in {b["biller_code"] for b in all_billers}


def test_pay_bill_posts_balanced_double_entry_and_records_audit_and_notification(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=500_000)
    assert alice.account_id is not None

    resp = client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, **new_key()},
        json={
            "from_account_id": str(alice.account_id),
            "biller_id": str(MSEB_BILLER_ID),
            "consumer_reference": "CA-987654321",
            "amount_minor": 125_000,
            "currency": "INR",
        },
    )
    assert resp.status_code == 201, resp.text
    assert "idempotent-replayed" not in resp.headers
    body = resp.json()
    assert body["status"] == "POSTED"
    assert body["amount_minor"] == 125_000
    assert body["currency"] == "INR"
    assert body["biller_id"] == str(MSEB_BILLER_ID)
    assert body["biller_code"] == "MSEB-MH"
    assert body["biller_name"] == "Maharashtra State Electricity Board"
    assert body["consumer_reference"] == "CA-987654321"
    assert body["reference"] == "BILL/MSEB-MH/CA-987654321"
    assert body["new_balance_minor"] == 375_000

    db.expire_all()
    customer_acc = db.get_one(Account, alice.account_id)
    clearing_acc = db.get_one(Account, ledger.SYSTEM_CLEARING_ID)
    assert customer_acc.ledger_balance == 375_000
    assert ledger.entry_balance(db, customer_acc.id) == customer_acc.ledger_balance
    assert ledger.entry_balance(db, clearing_acc.id) == clearing_acc.ledger_balance

    txn_id = uuid.UUID(body["transaction_id"])
    txn = db.get_one(Transaction, txn_id)
    assert txn.type == TransactionType.BILL_PAYMENT
    assert txn.status == TransactionStatus.POSTED
    assert txn.amount == 125_000
    assert txn.debit_account_id == alice.account_id
    assert txn.credit_account_id == ledger.SYSTEM_CLEARING_ID
    assert txn.reference == "BILL/MSEB-MH/CA-987654321"

    entries = list(
        db.execute(select(TransactionEntry).where(TransactionEntry.transaction_id == txn_id))
        .scalars()
        .all()
    )
    assert len(entries) == 2
    by_dir = {e.direction: e for e in entries}
    assert by_dir[EntryDirection.DEBIT].account_id == alice.account_id
    assert by_dir[EntryDirection.DEBIT].amount == 125_000
    assert by_dir[EntryDirection.DEBIT].balance_after == 375_000
    assert by_dir[EntryDirection.CREDIT].account_id == ledger.SYSTEM_CLEARING_ID
    assert by_dir[EntryDirection.CREDIT].amount == 125_000

    audit_row = db.execute(
        select(AuditLog).where(
            AuditLog.action == "BILL_PAYMENT_CREATED",
            AuditLog.resource_id == body["id"],
        )
    ).scalar_one_or_none()
    assert audit_row is not None and audit_row.actor_user_id == alice.user_id

    outbox_row = db.execute(
        select(OutboxEvent).where(OutboxEvent.event_type == notifications.BILL_PAYMENT_POSTED)
    ).scalar_one_or_none()
    assert outbox_row is not None
    relayed = notifications.relay_outbox(db)
    db.commit()
    assert relayed >= 1

    notif = db.execute(
        select(Notification).where(
            Notification.user_id == alice.user_id,
            Notification.type == notifications.BILL_PAYMENT_POSTED,
        )
    ).scalar_one_or_none()
    assert notif is not None
    assert notif.title == "Bill payment completed"
    assert "Maharashtra State Electricity Board" in notif.body


def test_get_and_list_bill_payments_preserve_receipt_fields(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=300_000)
    assert alice.account_id is not None

    first = client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, **new_key()},
        json={
            "from_account_id": str(alice.account_id),
            "biller_id": str(MSEB_BILLER_ID),
            "consumer_reference": "CA-11111",
            "amount_minor": 80_000,
        },
    ).json()
    second = client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, **new_key()},
        json={
            "from_account_id": str(alice.account_id),
            "biller_id": str(WATER_BILLER_ID),
            "consumer_reference": "WTR-22222",
            "amount_minor": 50_000,
        },
    ).json()

    fetched_first = client.get(f"/api/v1/bills/payments/{first['id']}", headers=alice.headers)
    assert fetched_first.status_code == 200
    assert fetched_first.json()["new_balance_minor"] == 220_000
    assert fetched_first.json()["reference"] == "BILL/MSEB-MH/CA-11111"

    listed = client.get("/api/v1/bills/payments", headers=alice.headers)
    assert listed.status_code == 200
    items = listed.json()
    assert [item["id"] for item in items] == [second["id"], first["id"]]
    assert items[0]["new_balance_minor"] == 170_000
    assert items[1]["new_balance_minor"] == 220_000


def test_pay_bill_insufficient_funds_leaves_zero_partial_state(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=50_000)
    assert alice.account_id is not None

    before_txns = db.execute(select(func.count()).select_from(Transaction)).scalar_one()
    before_entries = db.execute(select(func.count()).select_from(TransactionEntry)).scalar_one()

    resp = client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, **new_key()},
        json={
            "from_account_id": str(alice.account_id),
            "biller_id": str(MSEB_BILLER_ID),
            "consumer_reference": "CA-OVERDRAW",
            "amount_minor": 50_001,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "insufficient_funds"

    db.expire_all()
    assert db.get_one(Account, alice.account_id).ledger_balance == 50_000
    assert db.execute(select(func.count()).select_from(BillPayment)).scalar_one() == 0
    assert db.execute(select(func.count()).select_from(Transaction)).scalar_one() == before_txns
    assert (
        db.execute(select(func.count()).select_from(TransactionEntry)).scalar_one()
        == before_entries
    )


def test_pay_bill_rejects_inactive_or_nonexistent_biller_and_currency_mismatch(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=200_000)
    assert alice.account_id is not None

    missing_biller = client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, **new_key()},
        json={
            "from_account_id": str(alice.account_id),
            "biller_id": str(uuid.uuid4()),
            "consumer_reference": "CA-12345",
            "amount_minor": 10_000,
        },
    )
    assert missing_biller.status_code == 404
    assert missing_biller.json()["code"] == "not_found"

    biller = db.get_one(Biller, MSEB_BILLER_ID)
    biller.status = BillerStatus.INACTIVE
    db.commit()

    inactive_resp = client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, **new_key()},
        json={
            "from_account_id": str(alice.account_id),
            "biller_id": str(MSEB_BILLER_ID),
            "consumer_reference": "CA-12345",
            "amount_minor": 10_000,
        },
    )
    assert inactive_resp.status_code == 422
    assert inactive_resp.json()["code"] == "biller_inactive"

    wrong_currency = client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, **new_key()},
        json={
            "from_account_id": str(alice.account_id),
            "biller_id": str(WATER_BILLER_ID),
            "consumer_reference": "WTR-12345",
            "amount_minor": 10_000,
            "currency": "USD",
        },
    )
    assert wrong_currency.status_code == 422
    assert wrong_currency.json()["code"] == "unprocessable"


def test_pay_bill_validates_idempotency_header_and_consumer_reference_text(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=100_000)
    assert alice.account_id is not None

    no_header = client.post(
        "/api/v1/bills/pay",
        headers=alice.headers,
        json={
            "from_account_id": str(alice.account_id),
            "biller_id": str(MSEB_BILLER_ID),
            "consumer_reference": "CA-12345",
            "amount_minor": 10_000,
        },
    )
    assert no_header.status_code == 422

    for bad_ref in ("CA\x00123", "CA\n123", "CA\ud800123"):
        raw = json.dumps(
            {
                "from_account_id": str(alice.account_id),
                "biller_id": str(MSEB_BILLER_ID),
                "consumer_reference": bad_ref,
                "amount_minor": 10_000,
            }
        ).encode("ascii")
        bad_resp = client.post(
            "/api/v1/bills/pay",
            headers={**alice.headers, **new_key(), "Content-Type": "application/json"},
            content=raw,
        )
        assert bad_resp.status_code == 422, bad_resp.text
        assert bad_resp.json()["code"] == "validation_error"


def test_idempotent_retry_returns_original_receipt_and_increments_metric(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=300_000)
    assert alice.account_id is not None
    key = new_key()
    payload = {
        "from_account_id": str(alice.account_id),
        "biller_id": str(MSEB_BILLER_ID),
        "consumer_reference": "CA-IDEM-01",
        "amount_minor": 75_000,
    }
    before_replays = IDEMPOTENT_REPLAYS_TOTAL._value.get()

    first = client.post("/api/v1/bills/pay", headers={**alice.headers, **key}, json=payload)
    second = client.post("/api/v1/bills/pay", headers={**alice.headers, **key}, json=payload)
    third = client.post("/api/v1/bills/pay", headers={**alice.headers, **key}, json=payload)

    assert first.status_code == second.status_code == third.status_code == 201
    assert first.json() == second.json() == third.json()
    assert "idempotent-replayed" not in first.headers
    assert second.headers.get("idempotent-replayed") == "true"
    assert third.headers.get("idempotent-replayed") == "true"
    assert IDEMPOTENT_REPLAYS_TOTAL._value.get() == before_replays + 2

    db.expire_all()
    assert db.get_one(Account, alice.account_id).ledger_balance == 225_000
    assert db.execute(select(func.count()).select_from(BillPayment)).scalar_one() == 1

    # Reusing the same Idempotency-Key with a different payload must fail with 422
    mismatch = client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, **key},
        json={**payload, "amount_minor": 80_000},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == "idempotency_key_reuse"


def test_concurrent_duplicate_idempotency_key_creates_single_bill_payment(
    app: FastAPI,
    db: Session,
    make_customer: Callable[..., Actor],
) -> None:
    alice = make_customer(funds=500_000)
    assert alice.account_id is not None
    key = new_key()
    payload = {
        "from_account_id": str(alice.account_id),
        "biller_id": str(MSEB_BILLER_ID),
        "consumer_reference": "CA-CONC-DUP",
        "amount_minor": 60_000,
    }

    def fire(_: int) -> Any:
        with TestClient(app) as local_client:
            return local_client.post(
                "/api/v1/bills/pay",
                headers={**alice.headers, **key},
                json=payload,
            )

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(fire, range(10)))

    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["id"] for r in responses}) == 1
    assert sum("idempotent-replayed" not in r.headers for r in responses) == 1
    assert sum(r.headers.get("idempotent-replayed") == "true" for r in responses) == 9

    db.expire_all()
    assert db.get_one(Account, alice.account_id).ledger_balance == 440_000
    assert db.execute(select(func.count()).select_from(BillPayment)).scalar_one() == 1


def test_concurrent_competing_bill_payments_never_overdraw_and_keep_ledger_balanced(
    app: FastAPI,
    db: Session,
    migrated_engine: Engine,
    make_customer: Callable[..., Actor],
) -> None:
    alice = make_customer(funds=150_000)
    assert alice.account_id is not None

    def fire(idx: int) -> int:
        with TestClient(app) as local_client:
            resp = local_client.post(
                "/api/v1/bills/pay",
                headers={**alice.headers, **new_key()},
                json={
                    "from_account_id": str(alice.account_id),
                    "biller_id": str(MSEB_BILLER_ID),
                    "consumer_reference": f"CA-RACE-{idx:02d}",
                    "amount_minor": 30_000,
                },
            )
            return resp.status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = list(pool.map(fire, range(10)))

    assert statuses.count(201) == 5
    assert statuses.count(422) == 5

    db.expire_all()
    customer_acc = db.get_one(Account, alice.account_id)
    assert customer_acc.ledger_balance == 0
    assert ledger.entry_balance(db, customer_acc.id) == 0
    assert db.execute(select(func.count()).select_from(BillPayment)).scalar_one() == 5

    # Verify global double-entry balance across the entire ledger: SUM(debits) == SUM(credits)
    debits = db.execute(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (
                            TransactionEntry.direction == EntryDirection.DEBIT,
                            TransactionEntry.amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            )
        )
    ).scalar_one()
    credits = db.execute(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (
                            TransactionEntry.direction == EntryDirection.CREDIT,
                            TransactionEntry.amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            )
        )
    ).scalar_one()
    assert int(debits) == int(credits)
    _ = migrated_engine
