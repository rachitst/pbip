"""TASK-007: staff lockout inspection and manual unlock (ADMIN + MFA only, fully audited)."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fakeredis import FakeRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.redis import get_redis
from pbip_api.modules.auth.models import RoleName, SecurityEvent, SecurityEventType, User
from pbip_api.modules.compliance.models import AuditLog
from pbip_test_support import PASSWORD, Actor
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]

WRONG = "Definitely-Not-It-42"  # pragma: allowlist secret
NEW_PASSWORD = "Another-Strong-Pass-77!"  # pragma: allowlist secret


def _lock_out(client: TestClient, email: str, attempts: int = 5) -> None:
    for _ in range(attempts):
        bad = client.post("/api/v1/auth/login", json={"email": email, "password": WRONG})
        assert bad.status_code == 401


def _login_status(client: TestClient, email: str) -> int:
    return client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    ).status_code


def test_admin_inspects_and_unlocks_a_locked_customer(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    fake_redis: FakeRedis,
    db: Session,
) -> None:
    admin = make_staff(RoleName.ADMIN)
    alice = make_customer()
    _lock_out(client, alice.email)
    assert _login_status(client, alice.email) == 423
    assert fake_redis.get("rl:login:testclient") is not None

    status = client.get(f"/api/v1/admin/users/{alice.user_id}/lockout", headers=admin.headers)
    assert status.status_code == 200
    assert status.json()["is_locked"] is True
    assert status.json()["failed_login_count"] == 5
    assert status.json()["last_failure_at"] is not None

    unlocked = client.post(f"/api/v1/admin/users/{alice.user_id}/unlock", headers=admin.headers)
    assert unlocked.status_code == 200, unlocked.text
    body = unlocked.json()
    assert body["was_locked"] is True
    assert body["previous_failed_login_count"] == 5
    assert body["is_locked"] is False and body["failed_login_count"] == 0
    assert body["locked_until"] is None
    assert body["rate_limit_counters_cleared"] == 1
    assert fake_redis.get("rl:login:testclient") is None

    assert _login_status(client, alice.email) == 200  # usable immediately, no waiting

    user = db.get(User, alice.user_id)
    assert user is not None and user.failed_login_count == 0 and user.locked_until is None


def test_unlock_is_audited_with_admin_and_target_ids(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    admin = make_staff(RoleName.ADMIN)
    alice = make_customer()
    _lock_out(client, alice.email, attempts=2)
    assert (
        client.post(
            f"/api/v1/admin/users/{alice.user_id}/unlock", headers=admin.headers
        ).status_code
        == 200
    )

    audit = db.execute(select(AuditLog).where(AuditLog.action == "USER_UNLOCKED")).scalar_one()
    assert audit.actor_user_id == admin.user_id
    assert audit.resource_type == "user" and audit.resource_id == str(alice.user_id)
    assert audit.details["admin_user_id"] == str(admin.user_id)
    assert audit.details["target_user_id"] == str(alice.user_id)
    assert audit.details["previous_failed_login_count"] == 2
    assert audit.details["was_locked"] is False

    event = db.execute(
        select(SecurityEvent).where(
            SecurityEvent.user_id == alice.user_id,
            SecurityEvent.event_type == SecurityEventType.ACCOUNT_UNLOCKED,
        )
    ).scalar_one()
    assert event.details == {"by_admin": str(admin.user_id)}

    chain = client.get(
        "/api/v1/compliance/audit-logs/verify",
        headers=make_staff(RoleName.COMPLIANCE_OFFICER).headers,
    )
    assert chain.status_code == 200 and chain.json()["valid"] is True


@pytest.mark.parametrize(
    ("method", "suffix"), [("get", "lockout"), ("post", "unlock")], ids=["inspect", "unlock"]
)
def test_unknown_user_is_rfc7807_404(
    client: TestClient, make_staff: Callable[..., Actor], method: str, suffix: str
) -> None:
    admin = make_staff(RoleName.ADMIN)
    response = getattr(client, method)(
        f"/api/v1/admin/users/{uuid.uuid4()}/{suffix}", headers=admin.headers
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    problem: dict[str, Any] = response.json()
    assert problem["code"] == "not_found" and problem["status"] == 404


def test_unlock_rejects_a_malformed_user_id(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    admin = make_staff(RoleName.ADMIN)
    response = client.post("/api/v1/admin/users/not-a-uuid/unlock", headers=admin.headers)
    assert response.status_code == 422


@pytest.mark.parametrize(("method", "suffix"), [("get", "lockout"), ("post", "unlock")])
def test_only_mfa_verified_admins_may_inspect_or_unlock(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
    settings: Settings,
    method: str,
    suffix: str,
) -> None:
    # This test logs in five staff actors after five failed attempts from the same client IP;
    # lift the per-IP login limit so the limiter does not mask the authorization result.
    settings.rate_limit_login_per_minute = 50
    victim = make_customer()
    _lock_out(client, victim.email)
    path = f"/api/v1/admin/users/{victim.user_id}/{suffix}"

    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers=victim.headers).status_code == 403
    for role in (RoleName.FRAUD_ANALYST, RoleName.COMPLIANCE_OFFICER):
        assert getattr(client, method)(path, headers=make_staff(role).headers).status_code == 403
    no_mfa = getattr(client, method)(path, headers=make_staff(RoleName.ADMIN, mfa=False).headers)
    assert no_mfa.status_code == 403 and no_mfa.json()["code"] == "mfa_required"

    db.expire_all()
    still = db.get(User, victim.user_id)
    assert still is not None and still.locked_until is not None  # nobody above unlocked it


class _RedisDown(FakeRedis):
    def delete(self, *_names: Any) -> int:
        raise RedisConnectionError("redis unavailable")


def test_unlock_still_clears_the_db_lockout_when_redis_is_down(
    app: FastAPI,
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
) -> None:
    admin = make_staff(RoleName.ADMIN)
    alice = make_customer()
    _lock_out(client, alice.email)
    app.dependency_overrides[get_redis] = lambda: _RedisDown(decode_responses=True)

    response = client.post(f"/api/v1/admin/users/{alice.user_id}/unlock", headers=admin.headers)
    assert response.status_code == 200
    assert response.json()["rate_limit_counters_cleared"] is None
    assert response.json()["is_locked"] is False
    assert _login_status(client, alice.email) == 200


def test_unlock_does_not_reset_the_limiter_of_an_ip_spraying_other_accounts(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    """Review finding: an IP that failed against several users is spraying; unlocking one
    victim must not clear that IP's rate-limit window as a side effect."""
    settings.rate_limit_login_per_minute = 50
    admin = make_staff(RoleName.ADMIN)
    alice, bob = make_customer(), make_customer()
    _lock_out(client, alice.email)
    _lock_out(client, bob.email, attempts=1)  # same client IP also failed against Bob

    unlocked = client.post(f"/api/v1/admin/users/{alice.user_id}/unlock", headers=admin.headers)
    assert unlocked.status_code == 200
    assert unlocked.json()["is_locked"] is False  # the account itself is unlocked
    assert unlocked.json()["rate_limit_counters_cleared"] == 0
    assert fake_redis.get("rl:login:testclient") is not None  # the spraying IP keeps its limit


def test_wrong_current_password_is_logged_distinctly_from_a_login_failure(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer()
    response = client.post(
        "/api/v1/auth/password-change",
        headers=alice.headers,
        json={"current_password": WRONG, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 403
    reasons = db.execute(
        select(SecurityEvent.details["reason"].astext).where(
            SecurityEvent.user_id == alice.user_id,
            SecurityEvent.event_type == SecurityEventType.LOGIN_FAILURE,
        )
    ).scalars()
    assert list(reasons) == ["bad_current_password"]
