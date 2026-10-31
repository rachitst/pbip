import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.notifications.service import relay_outbox
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def _transfer(client: TestClient, alice: Actor, payee: dict[str, Any], amount: int = 500) -> Any:
    return client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": amount,
            "payee_id": payee["id"],
        },
    )


def test_audit_chain_verifies_and_detects_tampering(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
) -> None:
    make_customer(), make_customer()
    officer = make_staff(RoleName.COMPLIANCE_OFFICER)
    ok = client.get("/api/v1/compliance/audit-logs/verify", headers=officer.headers).json()
    assert ok["valid"] is True and ok["checked"] >= 3

    logs = client.get("/api/v1/compliance/audit-logs?limit=2", headers=officer.headers).json()
    assert logs[0]["seq"] > logs[1]["seq"] and len(logs[0]["hash"]) == 64

    # A DB superuser who disables the trigger and edits history still cannot hide it.
    with migrated_engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_immutable"))
        connection.execute(
            text(
                "UPDATE audit_logs SET action = 'HACKED' WHERE seq = (SELECT min(seq) FROM audit_logs)"
            )
        )
        connection.execute(text("ALTER TABLE audit_logs ENABLE TRIGGER audit_logs_immutable"))
    broken = client.get("/api/v1/compliance/audit-logs/verify", headers=officer.headers).json()
    assert broken["valid"] is False and broken["first_invalid_seq"] is not None


def test_audit_rows_are_append_only(
    db: Session, client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    make_customer()
    assert AuditService(db).verify_chain().valid
    with (
        migrated_engine.connect() as connection,
        pytest.raises((IntegrityError, DBAPIError), match="append-only"),
    ):
        connection.execute(text("DELETE FROM audit_logs"))


def test_outbox_relay_creates_notifications_exactly_once_and_only_for_the_owner(
    client: TestClient,
    db: Session,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice, bob = make_customer(funds=100_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    assert _transfer(client, alice, payee).status_code == 201
    assert (
        client.get("/api/v1/notifications", headers=alice.headers).json()["items"] == []
    )  # not relayed yet

    assert relay_outbox(db) == 1
    db.commit()
    assert relay_outbox(db) == 0  # already processed
    mine = client.get("/api/v1/notifications", headers=alice.headers).json()
    assert (
        len(mine["items"]) == 1
        and mine["items"][0]["title"] == "Transfer completed"
        and "INR 5.00" in mine["items"][0]["body"]
    )
    assert client.get("/api/v1/notifications", headers=bob.headers).json()["items"] == []
    assert (
        client.post(
            f"/api/v1/notifications/{mine['items'][0]['id']}/read", headers=bob.headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/notifications/{mine['items'][0]['id']}/read", headers=alice.headers
        ).status_code
        == 204
    )
    assert (
        client.get("/api/v1/notifications?unread_only=true", headers=alice.headers).json()["items"]
        == []
    )


def test_oversized_before_seq_is_a_validation_error_not_a_server_error(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    """Same class of defect as the notifications offset: bound the keyset cursor to a bigint."""
    officer = make_staff(RoleName.COMPLIANCE_OFFICER)
    too_big = 9_223_372_036_854_775_808
    resp = client.get(
        f"/api/v1/compliance/audit-logs?before_seq={too_big}", headers=officer.headers
    )
    assert resp.status_code == 422, resp.text
    ok = client.get(
        f"/api/v1/compliance/audit-logs?before_seq={too_big - 1}", headers=officer.headers
    )
    assert ok.status_code == 200


def test_compliance_audit_logs_role_and_mfa_authorization(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
) -> None:
    """TASK-048: verify COMPLIANCE_OFFICER and ADMIN with MFA, and deny unauthorized roles."""
    officer = make_staff(RoleName.COMPLIANCE_OFFICER, mfa=True)
    admin = make_staff(RoleName.ADMIN, mfa=True)
    analyst = make_staff(RoleName.FRAUD_ANALYST, mfa=True)
    officer_no_mfa = make_staff(RoleName.COMPLIANCE_OFFICER, mfa=False)
    customer = make_customer()

    # Authorized: Compliance Officer and Admin with MFA
    assert client.get("/api/v1/compliance/audit-logs", headers=officer.headers).status_code == 200
    assert (
        client.get("/api/v1/compliance/audit-logs/verify", headers=officer.headers).status_code
        == 200
    )
    assert client.get("/api/v1/compliance/audit-logs", headers=admin.headers).status_code == 200
    assert (
        client.get("/api/v1/compliance/audit-logs/verify", headers=admin.headers).status_code == 200
    )

    # Denied: unauthorized staff role (Fraud Analyst)
    assert client.get("/api/v1/compliance/audit-logs", headers=analyst.headers).status_code == 403
    assert (
        client.get("/api/v1/compliance/audit-logs/verify", headers=analyst.headers).status_code
        == 403
    )

    # Denied: customer role
    assert client.get("/api/v1/compliance/audit-logs", headers=customer.headers).status_code == 403
    assert (
        client.get("/api/v1/compliance/audit-logs/verify", headers=customer.headers).status_code
        == 403
    )

    # Denied: missing MFA session
    assert (
        client.get("/api/v1/compliance/audit-logs", headers=officer_no_mfa.headers).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/compliance/audit-logs/verify", headers=officer_no_mfa.headers
        ).status_code
        == 403
    )

    # IP field is exposed in the serialized output and remains nullable
    logs = client.get("/api/v1/compliance/audit-logs?limit=5", headers=officer.headers).json()
    assert len(logs) > 0
    assert "ip" in logs[0]
