"""TASK-094: editing a payee, and why a renamed payee must be verified again."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.compliance.models import AuditLog
from pbip_test_support import Actor
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


@pytest.fixture
def owned(
    make_customer: Callable[..., Actor], make_verified_payee: Callable[..., dict[str, Any]]
) -> tuple[Actor, Actor, dict[str, Any]]:
    alice, bob = make_customer(funds=1_000_000), make_customer()
    return alice, bob, make_verified_payee(alice, bob)


def patch(client: TestClient, actor: Actor, payee_id: str, body: dict[str, Any]) -> Any:
    return client.patch(f"/api/v1/payees/{payee_id}", headers=actor.headers, json=body)


def send(client: TestClient, actor: Actor, payee_id: str) -> Any:
    return client.post(
        "/api/v1/transfers",
        headers={**actor.headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"from_account_id": str(actor.account_id), "amount_minor": 100, "payee_id": payee_id},
    )


def audit_rows(engine: Engine, payee_id: str) -> list[AuditLog]:
    with Session(engine) as session:
        return list(
            session.execute(
                select(AuditLog).where(
                    AuditLog.resource_type == "payee", AuditLog.resource_id == payee_id
                )
            ).scalars()
        )


def test_nickname_change_keeps_the_payee_verified_and_is_audited(
    client: TestClient, owned: tuple[Actor, Actor, dict[str, Any]], migrated_engine: Engine
) -> None:
    alice, _, payee = owned
    response = patch(client, alice, payee["id"], {"nickname": "Landlord"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["nickname"] == "Landlord" and body["status"] == "VERIFIED"
    rows = audit_rows(migrated_engine, payee["id"])
    assert [r.action for r in rows] == ["PAYEE_UPDATED"]
    # Field names only: the new nickname itself is not copied into the audit trail.
    assert rows[0].details == {"fields": ["nickname"], "reverification_required": False}


def test_renaming_resets_verification_and_blocks_transfers_until_verified_again(
    client: TestClient, owned: tuple[Actor, Actor, dict[str, Any]]
) -> None:
    alice, _, payee = owned
    renamed = patch(client, alice, payee["id"], {"name": "Someone Else Entirely"})
    assert renamed.status_code == 200
    assert renamed.json()["status"] == "PENDING_VERIFICATION"
    assert renamed.json()["verified_at"] is None
    assert send(client, alice, payee["id"]).status_code == 422

    # The new name does not match the real holder, so verification keeps it blocked...
    check = client.post(f"/api/v1/payees/{payee['id']}/verify", headers=alice.headers)
    assert check.json()["name_match"] == "NO_MATCH"
    assert send(client, alice, payee["id"]).status_code == 422

    # ...and the correct name, verified again, makes it usable.
    assert patch(client, alice, payee["id"], {"name": "Test Customer"}).status_code == 200
    again = client.post(f"/api/v1/payees/{payee['id']}/verify", headers=alice.headers)
    assert again.json()["name_match"] == "MATCH"
    assert send(client, alice, payee["id"]).status_code in (201, 202)


def test_same_name_is_not_a_change(
    client: TestClient, owned: tuple[Actor, Actor, dict[str, Any]], migrated_engine: Engine
) -> None:
    alice, _, payee = owned
    response = patch(client, alice, payee["id"], {"name": "  Test Customer "})
    assert response.status_code == 200 and response.json()["status"] == "VERIFIED"
    assert audit_rows(migrated_engine, payee["id"]) == []


@pytest.mark.parametrize(
    "body",
    [
        {"account_number": "000000000000"},
        {"bank_code": "OTHER001"},
        {"name": "x"},
        {"name": None},
        {"nickname": "n" * 81},
        {"customer_id": str(uuid.uuid4())},
    ],
)
def test_account_details_are_immutable_and_input_is_validated(
    client: TestClient, owned: tuple[Actor, Actor, dict[str, Any]], body: dict[str, Any]
) -> None:
    alice, _, payee = owned
    assert patch(client, alice, payee["id"], body).status_code == 422
    unchanged = client.get(f"/api/v1/payees/{payee['id']}", headers=alice.headers).json()
    assert unchanged == payee


def test_another_customer_cannot_edit_and_gets_404(
    client: TestClient,
    owned: tuple[Actor, Actor, dict[str, Any]],
    make_customer: Callable[..., Actor],
) -> None:
    alice, _, payee = owned
    mallory = make_customer()
    assert patch(client, mallory, payee["id"], {"name": "Mallory"}).status_code == 404
    assert patch(client, mallory, str(uuid.uuid4()), {"name": "Mallory"}).status_code == 404
    unchanged = client.get(f"/api/v1/payees/{payee['id']}", headers=alice.headers).json()
    assert unchanged["name"] == "Test Customer" and unchanged["status"] == "VERIFIED"


def test_a_removed_payee_with_history_cannot_be_edited(
    client: TestClient, owned: tuple[Actor, Actor, dict[str, Any]]
) -> None:
    alice, _, payee = owned
    assert send(client, alice, payee["id"]).status_code in (201, 202)
    assert client.delete(f"/api/v1/payees/{payee['id']}", headers=alice.headers).status_code == 204
    assert patch(client, alice, payee["id"], {"nickname": "back"}).status_code == 409
