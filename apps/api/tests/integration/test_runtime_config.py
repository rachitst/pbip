"""TASK-108 / ADR-0031: ADMIN-only runtime configuration of allow-listed business limits.

Proves that a change is bounded, audited (who, old, new, reason) in the hash chain, serialised,
and ENFORCED on the very next transfer, bill payment and standing-order creation.
"""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.core.errors import UnprocessableError
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.runtime_config.allowlist import ALLOWLIST, RuntimeSettingKey
from pbip_api.modules.runtime_config.models import RuntimeSetting
from pbip_api.modules.runtime_config.service import (
    AUDIT_ACTION,
    RuntimeConfigService,
    effective_settings,
)
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

pytestmark = [pytest.mark.integration, pytest.mark.security]

URL = "/api/v1/admin/settings"
DAILY = "daily_transfer_limit_minor"
PER_TRANSFER = "max_transfer_minor"
SCHEDULES = "max_active_scheduled_transfers_per_customer"
REASON = "Tightening limits during the fraud review"
MSEB_BILLER_ID = "00000000-0000-0000-0000-0000000000b1"


def _key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


@pytest.fixture
def pair(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> tuple[Actor, Actor, dict[str, Any]]:
    alice, bob = make_customer(funds=10_000_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)  # baseline fraud rules stay quiet
    return alice, bob, payee


def _send(client: TestClient, alice: Actor, payee: dict[str, Any], amount: int) -> Any:
    return client.post(
        "/api/v1/transfers",
        headers={**alice.headers, **_key()},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": amount,
            "payee_id": payee["id"],
            "reference": "rent",
        },
    )


def _pay_bill(client: TestClient, alice: Actor, amount: int) -> Any:
    return client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, **_key()},
        json={
            "from_account_id": str(alice.account_id),
            "biller_id": MSEB_BILLER_ID,
            "consumer_reference": "CA-987654321",
            "amount_minor": amount,
            "currency": "INR",
        },
    )


def _schedule(client: TestClient, alice: Actor, payee: dict[str, Any], amount: int) -> Any:
    return client.post(
        "/api/v1/transfers/scheduled",
        headers={**alice.headers, **_key()},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": amount,
            "payee_id": payee["id"],
            "frequency": "MONTHLY",
            "start_date": (utcnow() + timedelta(days=1)).date().isoformat(),
        },
    )


def _change(client: TestClient, admin: Actor, key: str, value: Any, reason: str = REASON) -> Any:
    return client.put(
        f"{URL}/{key}", headers=admin.headers, json={"value": value, "reason": reason}
    )


def _audit_rows(db: Session) -> list[AuditLog]:
    db.expire_all()
    stmt = select(AuditLog).where(AuditLog.action == AUDIT_ACTION).order_by(AuditLog.seq)
    return list(db.execute(stmt).scalars())


def test_list_shows_every_allow_listed_setting_with_bounds_and_env_defaults(
    client: TestClient, make_staff: Callable[..., Actor], settings: Settings
) -> None:
    admin = make_staff(RoleName.ADMIN)
    response = client.get(URL, headers=admin.headers)
    assert response.status_code == 200, response.text
    items = {item["key"]: item for item in response.json()["items"]}
    assert set(items) == {DAILY, PER_TRANSFER, SCHEDULES}
    daily = items[DAILY]
    assert daily["default_value"] == daily["effective_value"] == settings.daily_transfer_limit_minor
    assert daily["overridden"] is False and daily["updated_by_user_id"] is None
    assert (daily["unit"], daily["min_value"], daily["max_value"]) == (
        "MINOR_UNITS",
        10_000,
        1_000_000_000,
    )
    assert items[SCHEDULES]["unit"] == "COUNT"


def test_a_lowered_daily_limit_is_enforced_on_the_next_transfer_and_bill_payment(
    client: TestClient,
    make_staff: Callable[..., Actor],
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    admin = make_staff(RoleName.ADMIN)
    assert _send(client, alice, payee, 100_000).status_code == 201  # env limit: 5,00,000.00

    assert _change(client, admin, DAILY, 150_000).status_code == 200
    over = _send(client, alice, payee, 60_000)
    assert over.status_code == 422 and over.json()["code"] == "limit_exceeded"
    assert "daily" in over.json()["title"]
    bill = _pay_bill(client, alice, 60_000)
    assert bill.status_code == 422 and "daily" in bill.json()["title"]
    assert _send(client, alice, payee, 50_000).status_code == 201  # exactly at the new limit

    raised = _change(client, admin, DAILY, 400_000)
    assert raised.status_code == 200 and raised.json()["effective_value"] == 400_000
    assert _send(client, alice, payee, 60_000).status_code == 201


def test_a_lowered_per_transfer_limit_applies_to_transfers_bills_and_standing_orders(
    client: TestClient,
    make_staff: Callable[..., Actor],
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    admin = make_staff(RoleName.ADMIN)
    assert _change(client, admin, PER_TRANSFER, 20_000).status_code == 200
    for response in (
        _send(client, alice, payee, 20_001),
        _pay_bill(client, alice, 20_001),
        _schedule(client, alice, payee, 20_001),
    ):
        assert response.status_code == 422 and response.json()["code"] == "limit_exceeded"
    assert _send(client, alice, payee, 20_000).status_code == 201


def test_a_lowered_standing_order_cap_applies_to_the_next_creation(
    client: TestClient,
    make_staff: Callable[..., Actor],
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    admin = make_staff(RoleName.ADMIN)
    assert _schedule(client, alice, payee, 10_000).status_code == 201
    assert _change(client, admin, SCHEDULES, 1).status_code == 200
    assert _schedule(client, alice, payee, 10_000).status_code == 409


def test_every_change_is_audited_with_actor_old_new_and_reason_in_the_hash_chain(
    client: TestClient, make_staff: Callable[..., Actor], db: Session, settings: Settings
) -> None:
    admin = make_staff(RoleName.ADMIN)
    first = _change(client, admin, DAILY, 150_000)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["overridden"] is True and body["effective_value"] == 150_000
    assert body["updated_by_user_id"] == str(admin.user_id)
    assert (
        _change(client, admin, DAILY, 250_000, "Partial relaxation after review").status_code == 200
    )

    rows = _audit_rows(db)
    assert [(r.actor_user_id, r.resource_type, r.resource_id) for r in rows] == [
        (admin.user_id, "runtime_setting", DAILY)
    ] * 2
    assert rows[0].details == {
        "key": DAILY,
        "old_value": settings.daily_transfer_limit_minor,
        "new_value": 150_000,
        "default_value": settings.daily_transfer_limit_minor,
        "reason": REASON,
    }
    assert rows[1].details["old_value"] == 150_000 and rows[1].details["new_value"] == 250_000
    assert rows[1].details["reason"] == "Partial relaxation after review"
    assert all(r.request_id for r in rows)
    assert AuditService(db).verify_chain().valid


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        (DAILY, 9_999, REASON),  # below the allow-listed minimum
        (DAILY, 1_000_000_001, REASON),  # above the allow-listed maximum
        (SCHEDULES, 0, REASON),
        (SCHEDULES, 201, REASON),
        (DAILY, -5, REASON),
        (DAILY, 10**13, REASON),
        (DAILY, "150000", REASON),  # strict integers only: no coercion from text
        (DAILY, 150_000.5, REASON),  # never a float for money
        (DAILY, True, REASON),
        (DAILY, 150_000, "too short"),  # reason is 10-500 characters
        (DAILY, 150_000, "x" * 501),
        (DAILY, 150_000, "bad\x00reason here"),
        ("jwt_secret", 150_000, REASON),  # not allow-listed: secrets are never configurable
        ("login_max_failures", 50, REASON),  # security settings are not either
        ("cors_origins", 1, REASON),
    ],
)
def test_invalid_changes_are_422_and_leave_no_state(
    client: TestClient,
    make_staff: Callable[..., Actor],
    db: Session,
    key: str,
    value: Any,
    reason: str,
) -> None:
    admin = make_staff(RoleName.ADMIN)
    response = _change(client, admin, key, value, reason)
    assert response.status_code == 422, response.text
    assert str(value) not in response.json().get("title", "")  # no echo of the input
    assert db.execute(select(RuntimeSetting)).first() is None
    assert _audit_rows(db) == []


def test_unknown_fields_and_unchanged_values_are_rejected(
    client: TestClient, make_staff: Callable[..., Actor], db: Session, settings: Settings
) -> None:
    admin = make_staff(RoleName.ADMIN)
    extra = client.put(
        f"{URL}/{DAILY}",
        headers=admin.headers,
        json={"value": 150_000, "reason": REASON, "key": "jwt_secret"},
    )
    assert extra.status_code == 422
    same = _change(client, admin, DAILY, settings.daily_transfer_limit_minor)
    assert same.status_code == 422 and same.json()["code"] == "unprocessable"
    assert db.execute(select(RuntimeSetting)).first() is None and _audit_rows(db) == []


def test_only_mfa_verified_admins_may_read_or_change_settings(
    client: TestClient,
    make_staff: Callable[..., Actor],
    make_customer: Callable[..., Actor],
    db: Session,
    settings: Settings,
) -> None:
    settings.rate_limit_login_per_minute = 50  # several actors log in from the same client IP
    body = {"value": 150_000, "reason": REASON}
    assert client.get(URL).status_code == 401
    assert client.put(f"{URL}/{DAILY}", json=body).status_code == 401
    others = [make_customer()] + [
        make_staff(role)
        for role in (
            RoleName.FRAUD_ANALYST,
            RoleName.COMPLIANCE_OFFICER,
            RoleName.RELATIONSHIP_MANAGER,
        )
    ]
    for actor in others:
        assert client.get(URL, headers=actor.headers).status_code == 403
        assert client.put(f"{URL}/{DAILY}", headers=actor.headers, json=body).status_code == 403
    no_mfa = make_staff(RoleName.ADMIN, mfa=False)
    for response in (
        client.get(URL, headers=no_mfa.headers),
        client.put(f"{URL}/{DAILY}", headers=no_mfa.headers, json=body),
    ):
        assert response.status_code == 403 and response.json()["code"] == "mfa_required"
    assert db.execute(select(RuntimeSetting)).first() is None and _audit_rows(db) == []


def test_concurrent_changes_are_serialised_and_each_audit_row_has_the_true_old_value(
    make_staff: Callable[..., Actor], migrated_engine: Engine, settings: Settings, db: Session
) -> None:
    admins = [make_staff(RoleName.ADMIN) for _ in range(4)]
    values = [110_000, 120_000, 130_000, 140_000]
    start = threading.Barrier(len(admins))

    def change(args: tuple[Actor, int]) -> str:
        admin, value = args
        with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:
            start.wait()
            RuntimeConfigService(session, settings).change_setting(
                RuntimeSettingKey.DAILY_TRANSFER_LIMIT_MINOR,
                value,
                REASON,
                admin.user_id,
                RequestMeta(),
            )
            return "ok"

    with ThreadPoolExecutor(max_workers=len(admins)) as pool:
        assert list(pool.map(change, zip(admins, values, strict=True))) == ["ok"] * 4

    rows = _audit_rows(db)
    chain = [settings.daily_transfer_limit_minor] + [r.details["new_value"] for r in rows]
    assert [r.details["old_value"] for r in rows] == chain[:-1]  # no lost or invented update
    assert sorted(chain[1:]) == values
    stored = db.execute(select(RuntimeSetting.value)).scalar_one()
    assert stored == chain[-1]


def test_a_tampered_out_of_bounds_row_is_ignored_and_the_env_value_applies(
    make_staff: Callable[..., Actor], migrated_engine: Engine, settings: Settings, db: Session
) -> None:
    """Only a hand edit of the database can store this; it must never widen a limit."""
    admin = make_staff(RoleName.ADMIN)
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_settings (key, value, updated_by_user_id, updated_at) "
                "VALUES (:k, :v, :u, now())"
            ),
            {
                "k": DAILY,
                "v": ALLOWLIST[RuntimeSettingKey.DAILY_TRANSFER_LIMIT_MINOR].max_value + 1,
                "u": admin.user_id,
            },
        )
    effective = effective_settings(db, settings)
    assert effective.daily_transfer_limit_minor == settings.daily_transfer_limit_minor
    view = RuntimeConfigService(db, settings).list_settings()[0]
    assert view.overridden is False and view.effective_value == settings.daily_transfer_limit_minor


def test_effective_settings_returns_the_base_object_when_nothing_is_overridden(
    db: Session, settings: Settings
) -> None:
    assert effective_settings(db, settings) is settings


def test_the_key_check_constraint_refuses_a_non_allow_listed_key(
    make_staff: Callable[..., Actor], migrated_engine: Engine
) -> None:
    admin = make_staff(RoleName.ADMIN)
    with (
        pytest.raises(IntegrityError, match="ck_runtime_settings_key_valid"),
        migrated_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO runtime_settings (key, value, updated_by_user_id, updated_at) "
                "VALUES ('jwt_secret', 1, :u, now())"
            ),
            {"u": admin.user_id},
        )


def test_service_refuses_out_of_bounds_values_even_without_http(
    make_staff: Callable[..., Actor], db: Session, settings: Settings
) -> None:
    admin = make_staff(RoleName.ADMIN)
    with pytest.raises(UnprocessableError):
        RuntimeConfigService(db, settings).change_setting(
            RuntimeSettingKey.MAX_TRANSFER_MINOR, 1, REASON, admin.user_id, RequestMeta()
        )
