"""RBAC (who may call what) and BOLA/IDOR (whose objects they may touch)."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.notifications.models import Notification
from pbip_api.modules.payments.models import (
    ScheduledTransfer,
    ScheduledTransferStatus,
    ScheduleFrequency,
)
from pbip_test_support import PASSWORD, Actor, FakeClock
from sqlalchemy import select
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_endpoints_require_authentication(client: TestClient) -> None:
    for method, path in [
        ("get", "/api/v1/customers/me"),
        ("get", "/api/v1/customers/devices"),
        ("get", "/api/v1/customers/me/risk-profile"),
        ("put", "/api/v1/customers/me/risk-profile"),
        ("get", "/api/v1/customers/me/demographics"),
        ("put", "/api/v1/customers/me/date-of-birth"),
        ("delete", "/api/v1/customers/me/date-of-birth"),
        ("put", "/api/v1/customers/me/recommendations-consent"),
        ("get", "/api/v1/accounts"),
        ("get", "/api/v1/accounts/summary"),
        ("post", "/api/v1/accounts"),
        ("get", "/api/v1/payees"),
        ("get", "/api/v1/bills/billers"),
        ("post", "/api/v1/bills/pay"),
        ("get", "/api/v1/bills/payments"),
        ("get", f"/api/v1/bills/payments/{uuid.uuid4()}"),
        ("get", "/api/v1/transfers/scheduled"),
        ("post", "/api/v1/transfers/scheduled"),
        ("get", f"/api/v1/transfers/scheduled/{uuid.uuid4()}"),
        ("delete", f"/api/v1/transfers/scheduled/{uuid.uuid4()}"),
        ("post", f"/api/v1/transfers/scheduled/{uuid.uuid4()}/cancel"),
        ("post", f"/api/v1/transfers/scheduled/{uuid.uuid4()}/pause"),
        ("post", f"/api/v1/transfers/scheduled/{uuid.uuid4()}/resume"),
        ("get", "/api/v1/loans"),
        ("post", "/api/v1/loans/apply"),
        ("get", f"/api/v1/loans/{uuid.uuid4()}"),
        ("get", "/api/v1/wealth/products"),
        ("post", "/api/v1/wealth/fixed-deposits/quote"),
        ("post", "/api/v1/wealth/fixed-deposits/book"),
        ("get", "/api/v1/wealth/fixed-deposits"),
        ("get", f"/api/v1/wealth/fixed-deposits/{uuid.uuid4()}"),
        ("post", f"/api/v1/wealth/fixed-deposits/{uuid.uuid4()}/close"),
        ("get", "/api/v1/wealth/portfolio"),
        ("get", "/api/v1/fraud/alerts"),
        ("get", "/api/v1/fraud/stats"),
        ("get", "/api/v1/fraud/monitoring"),
        ("get", "/api/v1/fraud/demo-lab"),
        ("get", "/api/v1/compliance/audit-logs"),
        ("get", "/api/v1/compliance/exceptions"),
        ("post", f"/api/v1/compliance/exceptions/ACCOUNT_LOCKOUT/{uuid.uuid4()}/acknowledge"),
        ("post", f"/api/v1/compliance/exceptions/ACCOUNT_LOCKOUT/{uuid.uuid4()}/resolve"),
        ("get", "/api/v1/notifications"),
        ("post", "/api/v1/notifications/mark-all-read"),
        ("post", f"/api/v1/notifications/{uuid.uuid4()}/read"),
        ("post", "/api/v1/auth/password-change"),
        ("get", f"/api/v1/analytics/spending?account_id={uuid.uuid4()}"),
        ("get", f"/api/v1/analytics/trends?account_id={uuid.uuid4()}"),
        ("get", f"/api/v1/analytics/health-score?account_id={uuid.uuid4()}"),
        ("get", f"/api/v1/analytics/forecast?account_id={uuid.uuid4()}"),
        ("get", "/api/v1/analytics/segment"),
        ("post", "/api/v1/assistant/chat"),
        ("post", "/api/v1/assistant/chat/stream"),
        ("get", "/api/v1/assistant/messages"),
        ("get", "/api/v1/recommendations"),
        ("post", "/api/v1/events/batch"),
        ("get", "/api/v1/insights/customers"),
        ("get", "/api/v1/insights/behaviour"),
        ("get", f"/api/v1/insights/customers/{uuid.uuid4()}"),
        ("get", f"/api/v1/insights/customers/{uuid.uuid4()}/interactions"),
        ("post", f"/api/v1/insights/customers/{uuid.uuid4()}/interactions"),
        ("get", f"/api/v1/admin/users/{uuid.uuid4()}/lockout"),
        ("post", f"/api/v1/admin/users/{uuid.uuid4()}/unlock"),
        ("post", "/api/v1/admin/users"),
        ("get", "/api/v1/admin/settings"),
        ("put", "/api/v1/admin/settings/daily_transfer_limit_minor"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path


def test_customer_cannot_use_staff_endpoints(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    assert client.get("/api/v1/fraud/alerts", headers=alice.headers).status_code == 403
    assert client.get("/api/v1/fraud/monitoring", headers=alice.headers).status_code == 403
    assert client.get("/api/v1/fraud/demo-lab", headers=alice.headers).status_code == 403
    assert client.get("/api/v1/compliance/audit-logs", headers=alice.headers).status_code == 403
    assert client.get("/api/v1/compliance/exceptions", headers=alice.headers).status_code == 403
    assert client.get("/api/v1/insights/customers", headers=alice.headers).status_code == 403
    assert client.get("/api/v1/insights/behaviour", headers=alice.headers).status_code == 403
    own = f"/api/v1/insights/customers/{alice.customer_id}"
    assert client.get(own, headers=alice.headers).status_code == 403
    assert client.get(f"{own}/interactions", headers=alice.headers).status_code == 403
    assert (
        client.post(
            "/api/v1/admin/users",
            headers=alice.headers,
            json={
                "email": "e@example.com",
                "password": PASSWORD,
                "full_name": "Eve",
                "roles": ["ADMIN"],
            },
        ).status_code
        == 403
    )


def test_only_admins_with_mfa_may_use_admin_routes(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    settings: Settings,
) -> None:
    """TASK-108: every /api/v1/admin route (staff users, lockouts, runtime settings) is 403 for
    customers, every other staff role and an ADMIN whose session skipped the authenticator step."""
    settings.rate_limit_login_per_minute = 50  # several actors log in from the same client IP
    victim = make_customer()
    new_user = {
        "email": "rbac.created@bank.example",
        "password": PASSWORD,
        "full_name": "Rbac Created",
        "roles": ["FRAUD_ANALYST"],
    }
    change = {"value": 150_000, "reason": "RBAC probe, must be refused"}
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("post", "/api/v1/admin/users", new_user),
        ("get", f"/api/v1/admin/users/{victim.user_id}/lockout", None),
        ("post", f"/api/v1/admin/users/{victim.user_id}/unlock", None),
        ("get", "/api/v1/admin/settings", None),
        ("put", "/api/v1/admin/settings/daily_transfer_limit_minor", change),
    ]
    refused = [victim] + [
        make_staff(role)
        for role in (
            RoleName.FRAUD_ANALYST,
            RoleName.COMPLIANCE_OFFICER,
            RoleName.RELATIONSHIP_MANAGER,
        )
    ]
    refused.append(make_staff(RoleName.ADMIN, mfa=False))
    for actor in refused:
        for method, path, body in calls:
            kwargs: dict[str, Any] = {"headers": actor.headers}
            if body is not None:
                kwargs["json"] = body
            assert getattr(client, method)(path, **kwargs).status_code == 403, (actor.email, path)
    admin = make_staff(RoleName.ADMIN)
    assert client.get("/api/v1/admin/settings", headers=admin.headers).status_code == 200


def test_staff_have_no_customer_profile(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    assert client.get("/api/v1/customers/me", headers=analyst.headers).status_code == 403
    assert client.get("/api/v1/customers/devices", headers=analyst.headers).status_code == 403
    for path in ("/api/v1/customers/me/risk-profile", "/api/v1/customers/me/demographics"):
        assert client.get(path, headers=analyst.headers).status_code == 403, path
    assert (
        client.delete("/api/v1/customers/me/date-of-birth", headers=analyst.headers).status_code
        == 403
    )
    assert client.get("/api/v1/accounts", headers=analyst.headers).status_code == 403
    assert client.get("/api/v1/accounts/summary", headers=analyst.headers).status_code == 403
    assert client.get("/api/v1/bills/payments", headers=analyst.headers).status_code == 403
    assert client.get("/api/v1/transfers/scheduled", headers=analyst.headers).status_code == 403
    assert (
        client.post(
            f"/api/v1/transfers/scheduled/{uuid.uuid4()}/pause", headers=analyst.headers
        ).status_code
        == 403
    )
    assert (
        client.get(f"/api/v1/bills/payments/{uuid.uuid4()}", headers=analyst.headers).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/analytics/spending?account_id={uuid.uuid4()}", headers=analyst.headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/analytics/trends?account_id={uuid.uuid4()}", headers=analyst.headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/analytics/health-score?account_id={uuid.uuid4()}", headers=analyst.headers
        ).status_code
        == 403
    )
    assert client.get("/api/v1/analytics/segment", headers=analyst.headers).status_code == 403
    assert (
        client.get(
            f"/api/v1/analytics/forecast?account_id={uuid.uuid4()}", headers=analyst.headers
        ).status_code
        == 403
    )
    assert client.get("/api/v1/recommendations", headers=analyst.headers).status_code == 403
    assert (
        client.post(
            "/api/v1/assistant/chat", headers=analyst.headers, json={"message": "balance"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/assistant/chat/stream", headers=analyst.headers, json={"message": "balance"}
        ).status_code
        == 403
    )
    assert client.get("/api/v1/assistant/messages", headers=analyst.headers).status_code == 403
    assert (
        client.post(
            "/api/v1/events/batch",
            headers=analyst.headers,
            json={
                "events": [
                    {
                        "event_type": "page_view",
                        "event_timestamp": "2026-10-31T10:00:00Z",
                        "session_id": "sess-staff",
                    }
                ]
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/accounts",
            headers=analyst.headers,
            json={"account_type": "SAVINGS"},
        ).status_code
        == 403
    )


def test_staff_endpoints_demand_mfa_in_the_session(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    no_mfa = make_staff(RoleName.FRAUD_ANALYST, mfa=False)
    denied = client.get("/api/v1/fraud/alerts", headers=no_mfa.headers)
    assert denied.status_code == 403 and denied.json()["code"] == "mfa_required"
    denied = client.get("/api/v1/fraud/monitoring", headers=no_mfa.headers)
    assert denied.status_code == 403 and denied.json()["code"] == "mfa_required"
    denied = client.get("/api/v1/fraud/demo-lab", headers=no_mfa.headers)
    assert denied.status_code == 403 and denied.json()["code"] == "mfa_required"
    with_mfa = make_staff(RoleName.FRAUD_ANALYST)
    assert client.get("/api/v1/fraud/alerts", headers=with_mfa.headers).status_code == 200


def test_roles_are_separated_between_staff_functions(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    compliance = make_staff(RoleName.COMPLIANCE_OFFICER)
    assert client.get("/api/v1/compliance/audit-logs", headers=analyst.headers).status_code == 403
    assert client.get("/api/v1/fraud/alerts", headers=compliance.headers).status_code == 403
    assert client.get("/api/v1/fraud/monitoring", headers=compliance.headers).status_code == 403
    assert client.get("/api/v1/fraud/demo-lab", headers=compliance.headers).status_code == 403
    assert (
        client.get("/api/v1/compliance/audit-logs", headers=compliance.headers).status_code == 200
    )
    # TASK-100/101: exceptions are compliance work, behavioural analytics is RM work
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER)
    exceptions, behaviour = "/api/v1/compliance/exceptions", "/api/v1/insights/behaviour"
    ack = f"{exceptions}/ACCOUNT_LOCKOUT/{uuid.uuid4()}/acknowledge"
    for staff in (analyst, rm):
        assert client.get(exceptions, headers=staff.headers).status_code == 403
        assert client.post(ack, headers=staff.headers, json={"reason": "x" * 5}).status_code == 403
    for staff in (analyst, compliance):
        assert client.get(behaviour, headers=staff.headers).status_code == 403
    assert client.get(exceptions, headers=compliance.headers).status_code == 200
    assert client.get(behaviour, headers=rm.headers).status_code == 200


def test_admin_can_create_staff_and_it_is_audited(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    admin = make_staff(RoleName.ADMIN)
    created = client.post(
        "/api/v1/admin/users",
        headers=admin.headers,
        json={
            "email": "new.analyst@bank.example",
            "password": PASSWORD,
            "full_name": "New Analyst",
            "roles": ["FRAUD_ANALYST"],
        },
    )
    assert created.status_code == 201 and created.json()["roles"] == ["FRAUD_ANALYST"]
    bad_role = client.post(
        "/api/v1/admin/users",
        headers=admin.headers,
        json={
            "email": "x@bank.example",
            "password": PASSWORD,
            "full_name": "X Y",
            "roles": ["GOD"],
        },
    )
    assert bad_role.status_code == 422


def _victim_resources(
    client: TestClient, alice: Actor, bob: Actor, make_verified_payee: Callable[..., dict[str, Any]]
) -> dict[str, str]:
    payee = make_verified_payee(alice, bob)
    transfer = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 1000,
            "payee_id": payee["id"],
        },
    )
    assert transfer.status_code == 201, transfer.text
    device = client.post(
        "/api/v1/customers/devices",
        headers=alice.headers,
        json={"fingerprint": "alice-victim-device-fp"},
    )
    assert device.status_code == 201, device.text
    bill_payment = client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(alice.account_id),
            "biller_id": "00000000-0000-0000-0000-0000000000b1",
            "consumer_reference": "CA-VICTIM-01",
            "amount_minor": 500,
        },
    )
    assert bill_payment.status_code == 201, bill_payment.text
    scheduled = client.post(
        "/api/v1/transfers/scheduled",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 700,
            "payee_id": payee["id"],
            "frequency": "MONTHLY",
            "start_date": (utcnow() + timedelta(days=1)).date().isoformat(),
        },
    )
    assert scheduled.status_code == 201, scheduled.text
    return {
        "account": str(alice.account_id),
        "payee": payee["id"],
        "transfer": transfer.json()["id"],
        "device": device.json()["id"],
        "bill_payment": bill_payment.json()["id"],
        "scheduled": scheduled.json()["id"],
    }


def test_bola_customer_b_cannot_touch_customer_a_objects(
    client: TestClient,
    db: Session,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
) -> None:
    alice = make_customer(funds=100_000)
    bob = make_customer()
    mallory = make_customer()
    ids = _victim_resources(client, alice, bob, make_verified_payee)
    notification = Notification(user_id=alice.user_id, type="TEST", title="Private", body="x")
    db.add(notification)
    db.commit()
    h = mallory.headers
    key = lambda: {**h, "Idempotency-Key": uuid.uuid4().hex}

    attempts = {
        "get account": client.get(f"/api/v1/accounts/{ids['account']}", headers=h),
        "list account entries": client.get(
            f"/api/v1/accounts/{ids['account']}/transactions", headers=h
        ),
        "analytics spending for victim account": client.get(
            f"/api/v1/analytics/spending?account_id={ids['account']}", headers=h
        ),
        "analytics forecast for victim account": client.get(
            f"/api/v1/analytics/forecast?account_id={ids['account']}", headers=h
        ),
        "analytics forecast non-existent account": client.get(
            f"/api/v1/analytics/forecast?account_id={uuid.uuid4()}", headers=h
        ),
        "analytics trends for victim account": client.get(
            f"/api/v1/analytics/trends?account_id={ids['account']}", headers=h
        ),
        "analytics spending non-existent account": client.get(
            f"/api/v1/analytics/spending?account_id={uuid.uuid4()}", headers=h
        ),
        "analytics trends non-existent account": client.get(
            f"/api/v1/analytics/trends?account_id={uuid.uuid4()}", headers=h
        ),
        "analytics health-score for victim account": client.get(
            f"/api/v1/analytics/health-score?account_id={ids['account']}", headers=h
        ),
        "analytics health-score non-existent account": client.get(
            f"/api/v1/analytics/health-score?account_id={uuid.uuid4()}", headers=h
        ),
        "statement for victim account": client.get(
            f"/api/v1/accounts/{ids['account']}/statements",
            headers=h,
            params={"start_date": "2026-01-01", "end_date": "2026-01-31"},
        ),
        "statement for non-existent account": client.get(
            f"/api/v1/accounts/{uuid.uuid4()}/statements",
            headers=h,
            params={"start_date": "2026-01-01", "end_date": "2026-01-31"},
        ),
        "get payee": client.get(f"/api/v1/payees/{ids['payee']}", headers=h),
        "verify payee": client.post(f"/api/v1/payees/{ids['payee']}/verify", headers=h),
        "edit payee": client.patch(
            f"/api/v1/payees/{ids['payee']}", headers=h, json={"nickname": "mine now"}
        ),
        "edit non-existent payee": client.patch(
            f"/api/v1/payees/{uuid.uuid4()}", headers=h, json={"nickname": "x"}
        ),
        "delete payee": client.delete(f"/api/v1/payees/{ids['payee']}", headers=h),
        "get transfer": client.get(f"/api/v1/transfers/{ids['transfer']}", headers=h),
        "step-up transfer": client.post(
            f"/api/v1/transfers/{ids['transfer']}/step-up", headers=h, json={"code": "123456"}
        ),
        "transfer FROM victim account": client.post(
            "/api/v1/transfers",
            headers=key(),
            json={
                "from_account_id": ids["account"],
                "amount_minor": 100,
                "to_account_id": str(mallory.account_id),
            },
        ),
        "transfer TO victim account as own": client.post(
            "/api/v1/transfers",
            headers=key(),
            json={
                "from_account_id": str(mallory.account_id),
                "amount_minor": 100,
                "to_account_id": ids["account"],
            },
        ),
        "transfer using victim payee": client.post(
            "/api/v1/transfers",
            headers=key(),
            json={
                "from_account_id": str(mallory.account_id),
                "amount_minor": 100,
                "payee_id": ids["payee"],
            },
        ),
        "random uuid account": client.get(f"/api/v1/accounts/{uuid.uuid4()}", headers=h),
        "get victim device": client.get(f"/api/v1/customers/devices/{ids['device']}", headers=h),
        "get victim bill payment": client.get(
            f"/api/v1/bills/payments/{ids['bill_payment']}", headers=h
        ),
        "pay bill FROM victim account": client.post(
            "/api/v1/bills/pay",
            headers=key(),
            json={
                "from_account_id": ids["account"],
                "biller_id": "00000000-0000-0000-0000-0000000000b1",
                "consumer_reference": "CA-MALLORY",
                "amount_minor": 100,
            },
        ),
        "get victim scheduled transfer": client.get(
            f"/api/v1/transfers/scheduled/{ids['scheduled']}", headers=h
        ),
        "cancel victim scheduled transfer": client.delete(
            f"/api/v1/transfers/scheduled/{ids['scheduled']}", headers=h
        ),
        "cancel victim scheduled transfer (POST)": client.post(
            f"/api/v1/transfers/scheduled/{ids['scheduled']}/cancel", headers=h
        ),
        "pause victim scheduled transfer": client.post(
            f"/api/v1/transfers/scheduled/{ids['scheduled']}/pause", headers=h
        ),
        "resume victim scheduled transfer": client.post(
            f"/api/v1/transfers/scheduled/{ids['scheduled']}/resume", headers=h
        ),
        "non-existent scheduled transfer": client.get(
            f"/api/v1/transfers/scheduled/{uuid.uuid4()}", headers=h
        ),
        "schedule FROM victim account": client.post(
            "/api/v1/transfers/scheduled",
            headers=key(),
            json={
                "from_account_id": ids["account"],
                "amount_minor": 100,
                "to_account_id": str(mallory.account_id),
                "frequency": "DAILY",
                "start_date": utcnow().date().isoformat(),
            },
        ),
        "schedule using victim payee": client.post(
            "/api/v1/transfers/scheduled",
            headers=key(),
            json={
                "from_account_id": str(mallory.account_id),
                "amount_minor": 100,
                "payee_id": ids["payee"],
                "frequency": "DAILY",
                "start_date": utcnow().date().isoformat(),
            },
        ),
        "mark victim notification read": client.post(
            f"/api/v1/notifications/{notification.id}/read", headers=h
        ),
    }
    for name, response in attempts.items():
        assert response.status_code == 404, f"{name}: expected 404 got {response.status_code}"
    # And nothing of Alice's was disturbed.
    own = client.get(f"/api/v1/accounts/{ids['account']}", headers=alice.headers)
    assert own.status_code == 200 and own.json()["ledger_balance_minor"] == 98_500
    schedule = client.get(f"/api/v1/transfers/scheduled/{ids['scheduled']}", headers=alice.headers)
    assert schedule.status_code == 200 and schedule.json()["status"] == "ACTIVE"
    inbox = client.get("/api/v1/notifications", headers=alice.headers).json()
    assert inbox["unread_count"] == 1 and inbox["items"][0]["read"] is False


def test_bola_loans_are_only_visible_to_their_borrower(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    """TASK-047: loans are read through loans/policy with the AUTHENTICATED customer id."""
    alice = make_customer(funds=1_00_000_00)  # income for an eligible loan
    mallory = make_customer(funds=1_00_000_00)
    product = "10000000-0000-4000-a000-000000000001"  # personal loan (catalogue)

    def apply(actor: Actor, account_id: str) -> Any:
        return client.post(
            "/api/v1/loans/apply",
            headers={**actor.headers, "Idempotency-Key": uuid.uuid4().hex},
            json={
                "product_id": product,
                "amount_minor": 1_00_000_00,
                "tenure_months": 12,
                "account_id": account_id,
            },
        )

    victim = apply(alice, str(alice.account_id))
    assert victim.status_code == 201, victim.text
    loan_id = victim.json()["id"]
    h = mallory.headers
    attempts = {
        "get victim loan": client.get(f"/api/v1/loans/{loan_id}", headers=h),
        "get non-existent loan": client.get(f"/api/v1/loans/{uuid.uuid4()}", headers=h),
        "disburse INTO victim account": apply(mallory, str(alice.account_id)),
    }
    for name, response in attempts.items():
        assert response.status_code == 404, f"{name}: expected 404 got {response.status_code}"
    assert client.get("/api/v1/loans", headers=h).json() == []
    own = client.get(f"/api/v1/accounts/{alice.account_id}", headers=alice.headers).json()
    assert own["ledger_balance_minor"] == 2_00_000_00  # income + one loan, nothing from Mallory


def test_bola_fixed_deposits_are_only_visible_to_their_owner(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    """TASK-064: deposits are read through wealth/policy with the AUTHENTICATED customer id; a
    deposit cannot be booked from, read or closed into someone else's account."""
    alice = make_customer(funds=2_00_000_00)
    mallory = make_customer(funds=2_00_000_00)
    product = "20000000-0000-4000-a000-000000000002"  # FD-REGULAR (seeded catalogue)

    def book(actor: Actor, account_id: str) -> Any:
        return client.post(
            "/api/v1/wealth/fixed-deposits/book",
            headers={**actor.headers, "Idempotency-Key": uuid.uuid4().hex},
            json={
                "product_id": product,
                "amount_minor": 1_00_000_00,
                "tenure_months": 12,
                "account_id": account_id,
            },
        )

    victim = book(alice, str(alice.account_id))
    assert victim.status_code == 201, victim.text
    deposit_id = victim.json()["id"]
    h = mallory.headers
    close = {"Idempotency-Key": uuid.uuid4().hex}
    attempts = {
        "get victim deposit": client.get(f"/api/v1/wealth/fixed-deposits/{deposit_id}", headers=h),
        "get non-existent deposit": client.get(
            f"/api/v1/wealth/fixed-deposits/{uuid.uuid4()}", headers=h
        ),
        "close victim deposit": client.post(
            f"/api/v1/wealth/fixed-deposits/{deposit_id}/close", headers=h | close
        ),
        "book FROM victim account": book(mallory, str(alice.account_id)),
    }
    for name, response in attempts.items():
        assert response.status_code == 404, f"{name}: expected 404 got {response.status_code}"
    assert client.get("/api/v1/wealth/fixed-deposits", headers=h).json() == []
    assert client.get("/api/v1/wealth/portfolio", headers=h).json()["open_deposits"] == 0
    own = client.get(f"/api/v1/accounts/{alice.account_id}", headers=alice.headers).json()
    assert own["ledger_balance_minor"] == 1_00_000_00  # only her own booking moved her money
    mine = client.get(f"/api/v1/wealth/fixed-deposits/{deposit_id}", headers=alice.headers)
    assert mine.status_code == 200 and mine.json()["status"] == "ACTIVE"


def test_bola_forecast_neither_reads_nor_alerts_for_another_customers_account(
    client: TestClient,
    db: Session,
    clock: FakeClock,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
) -> None:
    """TASK-043: the projection reads obligations and sends its alert only for the AUTHENTICATED
    customer's own account; for anyone else's it is the same 404 as a missing account."""
    alice, mallory = make_customer(funds=2_000_00), make_customer(funds=2_000_00)
    payee = make_verified_payee(alice, mallory)
    clock.advance(20 * 24 * 3600)  # history for the forecast
    today = datetime.fromtimestamp(clock(), tz=UTC).date()
    db.add(
        ScheduledTransfer(
            customer_id=alice.customer_id,
            user_id=alice.user_id,
            from_account_id=alice.account_id,
            payee_id=uuid.UUID(payee["id"]),
            amount=1_900_00,  # leaves INR 100 at the month end: a low-balance alert for Alice
            currency="INR",
            reference="Victim rent",
            frequency=ScheduleFrequency.MONTHLY,
            status=ScheduledTransferStatus.ACTIVE,
            start_date=today,
            next_run_date=today,
        )
    )
    db.commit()
    url = f"/api/v1/analytics/forecast?account_id={alice.account_id}"

    stolen = client.get(url, headers=mallory.headers)
    assert stolen.status_code == 404 and "Victim rent" not in stolen.text
    own = client.get(
        f"/api/v1/analytics/forecast?account_id={mallory.account_id}", headers=mallory.headers
    ).json()
    assert own["obligations"] == []  # Alice's standing order is not projected on Mallory's account
    alerts = select(Notification).where(Notification.type == "PROJECTED_LOW_BALANCE")
    assert db.execute(alerts).scalars().all() == []

    assert client.get(url, headers=alice.headers).json()["end_of_month"]["low_balance"] is True
    assert [n.user_id for n in db.execute(alerts).scalars()] == [alice.user_id]


def test_listings_only_contain_the_callers_own_objects(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice, bob = make_customer(funds=10_000), make_customer(funds=10_000)
    listed = client.get("/api/v1/accounts", headers=alice.headers).json()
    assert [a["id"] for a in listed] == [str(alice.account_id)]
    assert str(bob.account_id) not in str(listed)
    # TASK-109: the total is Alice's balance alone, and a smuggled customer id is ignored.
    summary = client.get(
        f"/api/v1/accounts/summary?customer_id={bob.customer_id}", headers=alice.headers
    ).json()
    assert [(t["ledger_balance_minor"], t["account_count"]) for t in summary["totals"]] == [
        (10_000, 1)
    ]

    alice_dev = client.post(
        "/api/v1/customers/devices",
        headers=alice.headers,
        json={"fingerprint": "alice-device-own"},
    ).json()
    bob_dev = client.post(
        "/api/v1/customers/devices",
        headers=bob.headers,
        json={"fingerprint": "bob-device-own"},
    ).json()
    devices_listed = client.get("/api/v1/customers/devices", headers=alice.headers).json()
    assert [d["id"] for d in devices_listed] == [alice_dev["id"]]
    assert bob_dev["id"] not in [d["id"] for d in devices_listed]

    alice_bill = client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(alice.account_id),
            "biller_id": "00000000-0000-0000-0000-0000000000b1",
            "consumer_reference": "CA-ALICE-01",
            "amount_minor": 1_000,
        },
    ).json()
    bob_bill = client.post(
        "/api/v1/bills/pay",
        headers={**bob.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(bob.account_id),
            "biller_id": "00000000-0000-0000-0000-0000000000b1",
            "consumer_reference": "CA-BOB-01",
            "amount_minor": 1_000,
        },
    ).json()
    bills_listed = client.get("/api/v1/bills/payments", headers=alice.headers).json()
    assert [b["id"] for b in bills_listed] == [alice_bill["id"]]
    assert bob_bill["id"] not in [b["id"] for b in bills_listed]

    # TASK-097 / TASK-107: "me" routes have no id to swap, and a smuggled customer id is ignored.
    client.put(
        "/api/v1/customers/me/risk-profile",
        headers=alice.headers,
        json={
            "investment_horizon": "OVER_5_YEARS",
            "loss_reaction": "INVEST_MORE",
            "investment_goal": "MAXIMUM_GROWTH",
            "investment_experience": "EXTENSIVE",
            "emergency_fund": "OVER_6_MONTHS",
        },
    )
    client.put(
        "/api/v1/customers/me/date-of-birth",
        headers=alice.headers,
        json={"date_of_birth": "1970-05-05"},
    )
    as_alice = f"customer_id={alice.customer_id}"
    bob_risk = client.get(f"/api/v1/customers/me/risk-profile?{as_alice}", headers=bob.headers)
    assert bob_risk.status_code == 200 and bob_risk.json()["band"] is None
    bob_demo = client.get(f"/api/v1/customers/me/demographics?{as_alice}", headers=bob.headers)
    assert bob_demo.status_code == 200 and bob_demo.json()["date_of_birth"] is None
    assert (
        client.get("/api/v1/customers/me/demographics", headers=alice.headers).json()[
            "date_of_birth"
        ]
        == "1970-05-05"
    )
