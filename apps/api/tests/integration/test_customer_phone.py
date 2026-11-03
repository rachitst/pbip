"""#398: the customer's own mobile number, stored only encrypted (ADR-0042) and shown masked."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.crypto import FieldCipher
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.customers.models import Customer
from pbip_test_support import Actor
from sqlalchemy import select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

PHONE = "+919812345678"
OTHER_PHONE = "+447700900123"
URL = "/api/v1/customers/me/phone"


def _stored(db: Session, actor: Actor) -> str | None:
    db.expire_all()
    customer = db.get(Customer, actor.customer_id)
    assert customer is not None
    return customer.phone_enc


def _audits(db: Session, actor: Actor, action: str) -> list[AuditLog]:
    return list(
        db.execute(
            select(AuditLog).where(
                AuditLog.actor_user_id == actor.user_id, AuditLog.action == action
            )
        ).scalars()
    )


def test_phone_is_stored_encrypted_returned_masked_and_audited_without_the_number(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, settings: Settings
) -> None:
    alice = make_customer()

    resp = client.put(URL, headers=alice.headers, json={"phone": PHONE})

    assert resp.status_code == 200, resp.text
    assert resp.json()["phone_masked"] == "•" * 9 + "5678"
    assert PHONE not in resp.text and PHONE[1:] not in resp.text
    profile = client.get("/api/v1/customers/me", headers=alice.headers)
    assert profile.json()["phone_masked"] == "•" * 9 + "5678"
    assert PHONE not in profile.text

    token = _stored(db, alice)
    assert token is not None and PHONE[1:] not in token  # ciphertext, not the number
    cipher = FieldCipher(settings.field_encryption_key.get_secret_value())
    assert cipher.decrypt(token) == PHONE

    (audit,) = _audits(db, alice, "PHONE_SET")
    assert audit.details == {"replaced_existing": False}
    assert PHONE[-4:] not in str(audit.details)


def test_saving_the_same_number_is_not_re_audited_and_a_new_one_replaces_it(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer()
    client.put(URL, headers=alice.headers, json={"phone": PHONE})
    first = _stored(db, alice)

    assert client.put(URL, headers=alice.headers, json={"phone": PHONE}).status_code == 200
    assert _stored(db, alice) == first  # untouched, not re-encrypted
    assert len(_audits(db, alice, "PHONE_SET")) == 1

    replaced = client.put(URL, headers=alice.headers, json={"phone": OTHER_PHONE})
    assert replaced.json()["phone_masked"].endswith("0123")
    audits = _audits(db, alice, "PHONE_SET")
    assert sorted(str(a.details) for a in audits) == sorted(
        [str({"replaced_existing": False}), str({"replaced_existing": True})]
    )


def test_delete_erases_the_number_and_is_audited_once(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer()
    client.put(URL, headers=alice.headers, json={"phone": PHONE})

    assert client.delete(URL, headers=alice.headers).status_code == 204
    assert client.delete(URL, headers=alice.headers).status_code == 204  # idempotent

    assert _stored(db, alice) is None
    assert client.get("/api/v1/customers/me", headers=alice.headers).json()["phone_masked"] is None
    assert len(_audits(db, alice, "PHONE_DELETED")) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"phone": "9812345678"},  # no country code
        {"phone": "+0812345678"},  # country code cannot start with 0
        {"phone": "+91 98123 45678"},  # spaces
        {"phone": "+91-9812345678"},
        {"phone": "+1234567"},  # too short
        {"phone": "+1234567890123456"},  # 16 digits, over E.164's 15
        {"phone": "+91981234567\uff18"},  # a full-width digit
        {"phone": 919812345678},
        {"phone": PHONE, "customer_id": "00000000-0000-0000-0000-000000000000"},
        {},
    ],
)
def test_invalid_numbers_and_unknown_fields_are_rejected_and_nothing_is_stored(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, body: object
) -> None:
    alice = make_customer()
    resp = client.put(URL, headers=alice.headers, json=body)
    assert resp.status_code == 422
    assert _stored(db, alice) is None
    assert _audits(db, alice, "PHONE_SET") == []


def test_phone_routes_are_for_the_authenticated_customer_only(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    alice, bob = make_customer(), make_customer()
    assert client.put(URL, json={"phone": PHONE}).status_code == 401
    assert client.delete(URL).status_code == 401
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    assert client.put(URL, headers=analyst.headers, json={"phone": PHONE}).status_code == 403

    # A smuggled customer id in the query is ignored: Bob only ever writes his own row.
    smuggled = f"{URL}?customer_id={alice.customer_id}"
    assert client.put(smuggled, headers=bob.headers, json={"phone": PHONE}).status_code == 200
    assert client.delete(smuggled, headers=bob.headers).status_code == 204
    assert _stored(db, alice) is None
    client.put(URL, headers=alice.headers, json={"phone": OTHER_PHONE})
    client.delete(smuggled, headers=bob.headers)
    assert _stored(db, alice) is not None


def test_phone_changes_are_rate_limited_per_customer(
    client: TestClient, make_customer: Callable[..., Actor], settings: Settings
) -> None:
    settings.rate_limit_phone_change_per_hour = 2
    alice, bob = make_customer(), make_customer()
    statuses = [
        client.put(URL, headers=alice.headers, json={"phone": p}).status_code
        for p in (PHONE, OTHER_PHONE, PHONE)
    ]
    assert statuses == [200, 200, 429]
    assert client.delete(URL, headers=alice.headers).status_code == 429
    assert client.put(URL, headers=bob.headers, json={"phone": PHONE}).status_code == 200


def test_a_number_stored_under_the_old_key_still_reads_after_rotation(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
    settings: Settings,
) -> None:
    """MultiFernet: the new key goes first, the old one stays readable until the rotation script
    has re-encrypted every row (runbook §3.2)."""
    alice = make_customer()
    client.put(URL, headers=alice.headers, json={"phone": PHONE})
    old_key = settings.field_encryption_key.get_secret_value()
    settings.field_encryption_key = type(settings.field_encryption_key)(
        f"{Fernet.generate_key().decode()},{old_key}"
    )

    profile = client.get("/api/v1/customers/me", headers=alice.headers)

    assert profile.json()["phone_masked"] == "•" * 9 + "5678"
    token = _stored(db, alice)
    assert token is not None
    assert FieldCipher(settings.field_encryption_key.get_secret_value()).decrypt(token) == PHONE


def test_without_an_encryption_key_nothing_is_stored(
    client: TestClient, make_customer: Callable[..., Actor], db: Session, settings: Settings
) -> None:
    alice = make_customer()
    settings.field_encryption_key = type(settings.field_encryption_key)("")
    with pytest.raises(RuntimeError, match="FIELD_ENCRYPTION_KEY"):
        client.put(URL, headers=alice.headers, json={"phone": PHONE})
    assert _stored(db, alice) is None
