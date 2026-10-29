"""RBAC (who may call what) and BOLA/IDOR (whose objects they may touch)."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.notifications.models import Notification
from pbip_test_support import PASSWORD, Actor
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_endpoints_require_authentication(client: TestClient) -> None:
    for method, path in [
        ("get", "/api/v1/customers/me"),
        ("get", "/api/v1/customers/devices"),
        ("get", "/api/v1/accounts"),
        ("post", "/api/v1/accounts"),
        ("get", "/api/v1/payees"),
        ("get", "/api/v1/fraud/alerts"),
        ("get", "/api/v1/compliance/audit-logs"),
        ("get", "/api/v1/notifications"),
        ("post", "/api/v1/notifications/mark-all-read"),
        ("post", f"/api/v1/notifications/{uuid.uuid4()}/read"),
        ("post", "/api/v1/auth/password-change"),
        ("get", f"/api/v1/analytics/spending?account_id={uuid.uuid4()}"),
        ("get", f"/api/v1/analytics/trends?account_id={uuid.uuid4()}"),
        ("get", f"/api/v1/analytics/health-score?account_id={uuid.uuid4()}"),
        ("get", f"/api/v1/admin/users/{uuid.uuid4()}/lockout"),
        ("post", f"/api/v1/admin/users/{uuid.uuid4()}/unlock"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path


def test_customer_cannot_use_staff_endpoints(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    assert client.get("/api/v1/fraud/alerts", headers=alice.headers).status_code == 403
    assert client.get("/api/v1/compliance/audit-logs", headers=alice.headers).status_code == 403
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


def test_staff_have_no_customer_profile(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    assert client.get("/api/v1/customers/me", headers=analyst.headers).status_code == 403
    assert client.get("/api/v1/customers/devices", headers=analyst.headers).status_code == 403
    assert client.get("/api/v1/accounts", headers=analyst.headers).status_code == 403
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
    with_mfa = make_staff(RoleName.FRAUD_ANALYST)
    assert client.get("/api/v1/fraud/alerts", headers=with_mfa.headers).status_code == 200


def test_roles_are_separated_between_staff_functions(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    compliance = make_staff(RoleName.COMPLIANCE_OFFICER)
    assert client.get("/api/v1/compliance/audit-logs", headers=analyst.headers).status_code == 403
    assert client.get("/api/v1/fraud/alerts", headers=compliance.headers).status_code == 403
    assert (
        client.get("/api/v1/compliance/audit-logs", headers=compliance.headers).status_code == 200
    )


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
    return {
        "account": str(alice.account_id),
        "payee": payee["id"],
        "transfer": transfer.json()["id"],
        "device": device.json()["id"],
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
        "get payee": client.get(f"/api/v1/payees/{ids['payee']}", headers=h),
        "verify payee": client.post(f"/api/v1/payees/{ids['payee']}/verify", headers=h),
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
        "mark victim notification read": client.post(
            f"/api/v1/notifications/{notification.id}/read", headers=h
        ),
    }
    for name, response in attempts.items():
        assert response.status_code == 404, f"{name}: expected 404 got {response.status_code}"
    # And nothing of Alice's was disturbed.
    own = client.get(f"/api/v1/accounts/{ids['account']}", headers=alice.headers)
    assert own.status_code == 200 and own.json()["ledger_balance_minor"] == 99_000
    inbox = client.get("/api/v1/notifications", headers=alice.headers).json()
    assert inbox["unread_count"] == 1 and inbox["items"][0]["read"] is False


def test_listings_only_contain_the_callers_own_objects(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice, bob = make_customer(), make_customer()
    listed = client.get("/api/v1/accounts", headers=alice.headers).json()
    assert [a["id"] for a in listed] == [str(alice.account_id)]
    assert str(bob.account_id) not in str(listed)

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
