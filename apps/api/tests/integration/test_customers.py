"""Integration tests for Customer Profile and Device Fingerprint Registration API.

Verifies:
- GET /api/v1/customers/me returns authenticated customer profile details.
- Unauthenticated access returns 401.
- Staff users without customer profile return 403.
- Phone number is decrypted and masked (never exposed in plain text).
- POST /api/v1/customers/devices records device with SHA-256 fingerprint hash.
- Raw fingerprints and fingerprint hashes are never exposed in responses.
- New devices are untrusted (trusted=False) by default.
- Duplicate device registration touches last_seen_at while preserving first_seen_at and trusted status.
- Device age in days is correctly calculated.
- Registration creates an audit log entry in audit_logs.
- Listing / fetching devices strictly enforces customer isolation and BOLA (non-owned returns 404).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.crypto import FieldCipher
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.customers.models import Customer, Device
from pbip_api.modules.customers.service import mask_phone_number
from pbip_test_support import Actor
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_mask_phone_number_helper() -> None:
    """Verify phone masking hides all but the last 4 digits."""
    assert mask_phone_number("+919876543210") == "•••••••••3210"
    assert mask_phone_number("1234") == "1234"
    assert mask_phone_number("123") == "123"
    assert mask_phone_number("9876543210") == "••••••3210"


def test_get_customer_me_profile_success(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """Verify authenticated customer can retrieve their profile details."""
    alice = make_customer(email="alice.profile@example.com")
    resp = client.get("/api/v1/customers/me", headers=alice.headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["id"] == str(alice.customer_id)
    assert data["user_id"] == str(alice.user_id)
    assert data["email"] == "alice.profile@example.com"
    assert data["full_name"] == "Test Customer"
    assert data["kyc_status"] == "PENDING"
    assert data["phone_masked"] is None


def test_get_customer_me_with_masked_phone(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: pytest.MonkeyPatch,
) -> None:
    """Verify stored encrypted phone number is returned properly masked."""
    alice = make_customer()
    from cryptography.fernet import Fernet

    test_key = Fernet.generate_key().decode()
    cipher = FieldCipher(test_key)
    encrypted_phone = cipher.encrypt("+919876543210")

    from sqlalchemy import update

    with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:  # type: ignore[arg-type]
        session.execute(
            update(Customer)
            .where(Customer.id == alice.customer_id)
            .values(phone_enc=encrypted_phone)
        )
        session.commit()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "pbip_api.modules.customers.service.CustomerService._get_field_cipher",
            lambda _self: cipher,
        )
        resp = client.get("/api/v1/customers/me", headers=alice.headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_masked"] == "•••••••••3210"
        assert "+919876543210" not in resp.text


def test_get_customer_me_unauthenticated_returns_401(client: TestClient) -> None:
    """Verify unauthenticated requests to /customers/me return 401."""
    resp = client.get("/api/v1/customers/me")
    assert resp.status_code == 401


def test_get_customer_me_staff_without_profile_returns_403(
    client: TestClient,
    make_staff: Callable[..., Actor],
) -> None:
    """Verify bank staff without customer profile are rejected with 403."""
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    resp = client.get("/api/v1/customers/me", headers=analyst.headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"


def test_register_device_success(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: pytest.MonkeyPatch,
) -> None:
    """Verify POST /customers/devices registers device with SHA-256 and untrusted by default."""
    alice = make_customer()
    raw_fingerprint = "useragent-screen-canvas-webgl-hardware-12345"
    expected_hash = hashlib.sha256(raw_fingerprint.encode("utf-8")).hexdigest()

    resp = client.post(
        "/api/v1/customers/devices",
        headers=alice.headers,
        json={"fingerprint": raw_fingerprint, "label": "Alice's MacBook Pro"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()

    # Verify response schema
    assert "id" in data
    assert data["label"] == "Alice's MacBook Pro"
    assert data["trusted"] is False
    assert data["device_age_days"] == 0
    assert "first_seen_at" in data
    assert "last_seen_at" in data

    # Verify raw fingerprint and fingerprint hash are NOT leaked in response
    assert raw_fingerprint not in resp.text
    assert expected_hash not in resp.text

    # Verify DB record
    with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:  # type: ignore[arg-type]
        device = session.execute(
            select(Device).where(Device.id == uuid.UUID(data["id"]))
        ).scalar_one()
        assert device.customer_id == alice.customer_id
        assert device.fingerprint_hash == expected_hash
        assert device.trusted is False
        assert device.label == "Alice's MacBook Pro"


def test_register_device_via_header(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """Verify X-Device-ID header can supply fingerprint if omitted in body."""
    alice = make_customer()
    header_device_id = "device-uuid-9876543210"

    resp = client.post(
        "/api/v1/customers/devices",
        headers={**alice.headers, "X-Device-ID": header_device_id},
        json={"label": "Mobile App"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["label"] == "Mobile App"
    assert data["trusted"] is False


def test_register_device_missing_fingerprint_returns_422(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """Verify registration fails when neither fingerprint nor X-Device-ID is provided."""
    alice = make_customer()
    resp = client.post(
        "/api/v1/customers/devices",
        headers=alice.headers,
        json={"label": "No fingerprint device"},
    )
    assert resp.status_code == 422


def test_duplicate_device_registration_preserves_first_seen_and_trusted(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: pytest.MonkeyPatch,
) -> None:
    """Verify repeat device registration touches last_seen_at without resetting first_seen_at or trusted."""
    alice = make_customer()
    raw_fingerprint = "repeat-device-fingerprint-xyz"

    # Step 1: Register initially
    resp1 = client.post(
        "/api/v1/customers/devices",
        headers=alice.headers,
        json={"fingerprint": raw_fingerprint, "label": "Initial Label"},
    )
    assert resp1.status_code == 201
    device_id = uuid.UUID(resp1.json()["id"])
    first_seen_str = resp1.json()["first_seen_at"]

    # Step 2: Manually set trusted=True in DB to verify it is NOT overwritten
    with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:  # type: ignore[arg-type]
        dev = session.execute(select(Device).where(Device.id == device_id)).scalar_one()
        dev.trusted = True
        session.commit()

    # Step 3: Register same fingerprint again
    resp2 = client.post(
        "/api/v1/customers/devices",
        headers=alice.headers,
        json={"fingerprint": raw_fingerprint, "label": "Updated Label"},
    )
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()

    assert data2["id"] == str(device_id)
    assert data2["label"] == "Updated Label"
    assert data2["first_seen_at"] == first_seen_str
    assert data2["trusted"] is True


def test_device_registration_records_audit_trail(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: pytest.MonkeyPatch,
) -> None:
    """Verify device registration writes an audit event to audit_logs."""
    alice = make_customer()
    resp = client.post(
        "/api/v1/customers/devices",
        headers=alice.headers,
        json={"fingerprint": "audit-test-device-fp"},
    )
    assert resp.status_code == 201
    dev_id = resp.json()["id"]

    with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:  # type: ignore[arg-type]
        audit_log = session.execute(
            select(AuditLog).where(
                AuditLog.resource_type == "device",
                AuditLog.resource_id == dev_id,
            )
        ).scalar_one_or_none()
        assert audit_log is not None
        assert audit_log.actor_user_id == alice.user_id
        assert audit_log.action == "DEVICE_REGISTERED"
        assert audit_log.details["trusted"] is False
        assert audit_log.details["is_new"] is True


def test_list_and_get_devices_bola_isolation(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """Verify caller can only list their own devices and cannot access another customer's device (404)."""
    alice = make_customer()
    bob = make_customer()

    # Alice registers a device
    alice_dev_resp = client.post(
        "/api/v1/customers/devices",
        headers=alice.headers,
        json={"fingerprint": "alice-device-fp", "label": "Alice Laptop"},
    )
    alice_dev_id = alice_dev_resp.json()["id"]

    # Bob registers a device
    bob_dev_resp = client.post(
        "/api/v1/customers/devices",
        headers=bob.headers,
        json={"fingerprint": "bob-device-fp", "label": "Bob Phone"},
    )
    bob_dev_id = bob_dev_resp.json()["id"]

    # Alice lists devices: sees only her own
    alice_list = client.get("/api/v1/customers/devices", headers=alice.headers).json()
    assert len(alice_list) == 1
    assert alice_list[0]["id"] == alice_dev_id
    assert alice_list[0]["label"] == "Alice Laptop"

    # Alice gets her own device: 200
    alice_get = client.get(f"/api/v1/customers/devices/{alice_dev_id}", headers=alice.headers)
    assert alice_get.status_code == 200

    # Bob attempts to get Alice's device: 404 (BOLA protection)
    bob_touch_alice = client.get(f"/api/v1/customers/devices/{alice_dev_id}", headers=bob.headers)
    assert bob_touch_alice.status_code == 404

    # Alice attempts to get Bob's device: 404
    alice_touch_bob = client.get(f"/api/v1/customers/devices/{bob_dev_id}", headers=alice.headers)
    assert alice_touch_bob.status_code == 404
