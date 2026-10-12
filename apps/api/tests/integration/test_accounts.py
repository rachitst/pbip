"""Integration tests for Account Opening & Ledger Initialization API (TASK-005).

Verifies:
- POST /api/v1/accounts opens SAVINGS and CHECKING accounts (mapping CHECKING to CURRENT).
- New accounts have 12-digit compliant account numbers with prefix '40' and Luhn checksum.
- Accounts are linked to the authenticated customer as primary holder.
- Initial balances are zero and comply with double-entry ledger invariants.
- Active account limit of 5 is enforced per customer.
- Closed accounts do not count towards the 5 active account ceiling.
- Concurrent account creation attempts are serialized via advisory lock without exceeding limits.
- System account types and invalid inputs are rejected.
- Audit trail event ACCOUNT_OPENED is recorded in audit_logs.
- Unauthenticated access returns 401; staff access returns 403.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.modules.accounts.models import (
    Account,
    AccountHolder,
    AccountStatus,
    AccountType,
    HolderRole,
)
from pbip_api.modules.accounts.service import is_valid_account_number, mask_account_number
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.compliance.models import AuditLog
from pbip_test_support import Actor
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_open_savings_account_success(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
) -> None:
    alice = make_customer()
    resp = client.post(
        "/api/v1/accounts",
        headers=alice.headers,
        json={"account_type": "SAVINGS", "currency": "INR"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()

    acc_id = uuid.UUID(data["id"])
    account_number = data["account_number"]
    assert account_number.startswith("40")
    assert len(account_number) == 12
    assert is_valid_account_number(account_number)
    assert data["account_number_masked"] == mask_account_number(account_number)
    assert data["account_type"] == "SAVINGS"
    assert data["currency"] == "INR"
    assert data["status"] == "ACTIVE"
    assert data["ledger_balance_minor"] == 0
    assert data["held_minor"] == 0
    assert data["available_minor"] == 0

    holder = db.execute(
        select(AccountHolder).where(
            AccountHolder.account_id == acc_id,
            AccountHolder.customer_id == alice.customer_id,
        )
    ).scalar_one_or_none()
    assert holder is not None
    assert holder.holder_role == HolderRole.PRIMARY


def test_open_checking_account_maps_to_current(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
) -> None:
    alice = make_customer()
    resp = client.post(
        "/api/v1/accounts",
        headers=alice.headers,
        json={"account_type": "CHECKING"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["account_type"] == "CURRENT"
    assert data["status"] == "ACTIVE"
    assert data["ledger_balance_minor"] == 0

    acc_id = uuid.UUID(data["id"])
    account = db.get(Account, acc_id)
    assert account is not None
    assert account.account_type == AccountType.CURRENT


def test_open_current_account_success(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    alice = make_customer()
    resp = client.post(
        "/api/v1/accounts",
        headers=alice.headers,
        json={"account_type": "CURRENT"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["account_type"] == "CURRENT"


def test_open_account_case_insensitive(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    alice = make_customer()
    resp_savings = client.post(
        "/api/v1/accounts",
        headers=alice.headers,
        json={"account_type": "savings"},
    )
    assert resp_savings.status_code == 201
    assert resp_savings.json()["account_type"] == "SAVINGS"

    resp_checking = client.post(
        "/api/v1/accounts",
        headers=alice.headers,
        json={"account_type": "checking"},
    )
    assert resp_checking.status_code == 201
    assert resp_checking.json()["account_type"] == "CURRENT"


def test_open_account_enforces_limit_of_five(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    alice = make_customer()
    # Registration automatically creates 1 active account.
    initial_accounts = client.get("/api/v1/accounts", headers=alice.headers).json()
    assert len(initial_accounts) == 1

    # Open 4 more accounts to reach 5 active accounts.
    for i in range(4):
        acc_type = "CHECKING" if i % 2 == 0 else "SAVINGS"
        resp = client.post(
            "/api/v1/accounts",
            headers=alice.headers,
            json={"account_type": acc_type},
        )
        assert resp.status_code == 201, f"Failed opening account #{i + 2}: {resp.text}"

    all_accounts = client.get("/api/v1/accounts", headers=alice.headers).json()
    assert len(all_accounts) == 5

    # Attempting to open a 6th active account must fail.
    resp_sixth = client.post(
        "/api/v1/accounts",
        headers=alice.headers,
        json={"account_type": "SAVINGS"},
    )
    assert resp_sixth.status_code == 422
    problem = resp_sixth.json()
    assert problem["code"] == "account_limit_exceeded"
    assert "cannot have more than 5 active accounts" in problem["title"]

    # Verify count remains 5
    after_attempt = client.get("/api/v1/accounts", headers=alice.headers).json()
    assert len(after_attempt) == 5


def test_open_account_ignores_closed_accounts_for_limit(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
) -> None:
    alice = make_customer()
    # Open 4 more to reach the 5-account limit.
    for _ in range(4):
        r = client.post(
            "/api/v1/accounts",
            headers=alice.headers,
            json={"account_type": "SAVINGS"},
        )
        assert r.status_code == 201

    # Mark 1 account as CLOSED in the database.
    acc = db.execute(
        select(Account)
        .join(AccountHolder, AccountHolder.account_id == Account.id)
        .where(AccountHolder.customer_id == alice.customer_id)
        .limit(1)
    ).scalar_one()
    acc.status = AccountStatus.CLOSED
    db.commit()

    # Now active account count is 4. Opening a 5th active account must succeed.
    resp = client.post(
        "/api/v1/accounts",
        headers=alice.headers,
        json={"account_type": "SAVINGS"},
    )
    assert resp.status_code == 201, resp.text


def test_open_account_unauthenticated(client: TestClient) -> None:
    resp = client.post("/api/v1/accounts", json={"account_type": "SAVINGS"})
    assert resp.status_code == 401


def test_staff_cannot_open_customer_account(
    client: TestClient,
    make_staff: Callable[..., Actor],
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    resp = client.post(
        "/api/v1/accounts",
        headers=analyst.headers,
        json={"account_type": "SAVINGS"},
    )
    assert resp.status_code == 403


def test_open_account_invalid_inputs(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    alice = make_customer()

    # SYSTEM account type is forbidden
    resp_sys = client.post(
        "/api/v1/accounts",
        headers=alice.headers,
        json={"account_type": "SYSTEM"},
    )
    assert resp_sys.status_code == 422

    # Invalid account type
    resp_inv = client.post(
        "/api/v1/accounts",
        headers=alice.headers,
        json={"account_type": "INVESTMENT"},
    )
    assert resp_inv.status_code == 422

    # Empty payload
    resp_empty = client.post(
        "/api/v1/accounts",
        headers=alice.headers,
        json={},
    )
    assert resp_empty.status_code == 422

    # Invalid currency format
    resp_curr = client.post(
        "/api/v1/accounts",
        headers=alice.headers,
        json={"account_type": "SAVINGS", "currency": "US"},
    )
    assert resp_curr.status_code == 422


def test_open_account_audit_trail(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
) -> None:
    alice = make_customer()
    resp = client.post(
        "/api/v1/accounts",
        headers=alice.headers,
        json={"account_type": "SAVINGS"},
    )
    assert resp.status_code == 201
    acc_id = resp.json()["id"]

    log_entry = db.execute(
        select(AuditLog).where(
            AuditLog.resource_type == "account",
            AuditLog.resource_id == acc_id,
            AuditLog.action == "ACCOUNT_OPENED",
        )
    ).scalar_one_or_none()
    assert log_entry is not None
    assert log_entry.actor_user_id == alice.user_id
    assert log_entry.details["account_type"] == "SAVINGS"
    assert "account_number" in log_entry.details


def test_open_account_concurrent_safety(
    app: FastAPI,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
) -> None:
    alice = make_customer()
    # Customer starts with 1 account. Open 3 more sequentially -> now 4 active accounts.
    client = TestClient(app)
    for _ in range(3):
        r = client.post(
            "/api/v1/accounts",
            headers=alice.headers,
            json={"account_type": "SAVINGS"},
        )
        assert r.status_code == 201

    # Now Alice has exactly 4 accounts (1 slot left before hitting the limit of 5).
    # Fire 6 concurrent requests in separate threads.
    def fire(_: int) -> int:
        thread_client = TestClient(app)
        res = thread_client.post(
            "/api/v1/accounts",
            headers=alice.headers,
            json={"account_type": "CHECKING"},
        )
        return res.status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        status_codes = list(pool.map(fire, range(6)))

    # Exactly 1 request should succeed (201), bringing total to 5.
    # The other 5 requests must fail with 422 (account_limit_exceeded).
    success_count = sum(code == 201 for code in status_codes)
    limit_count = sum(code == 422 for code in status_codes)
    assert success_count == 1, f"Expected exactly 1 success, got {status_codes}"
    assert limit_count == 5, f"Expected 5 limit errors, got {status_codes}"

    with migrated_engine.connect() as conn:
        total_active = conn.execute(
            select(func.count(Account.id))
            .join(AccountHolder, AccountHolder.account_id == Account.id)
            .where(
                AccountHolder.customer_id == alice.customer_id,
                Account.status == AccountStatus.ACTIVE,
            )
        ).scalar_one()
        assert total_active == 5
