"""ADR-0021: access tokens stop working as soon as the session is revoked, not after their TTL.

Before this change a disabled user's (or a stolen) access token kept working for up to
JWT_ACCESS_TTL_MINUTES (threat model, known gap). Every authenticated request now checks the user's
status and token_version on the request's own database session.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.security import encode_token
from pbip_api.modules.auth.models import User, UserStatus
from pbip_test_support import PASSWORD, Actor, login_json
from sqlalchemy import update
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

NEW_PASSWORD = "Correct-Horse-New-9!"  # pragma: allowlist secret


def _me(client: TestClient, token: str) -> int:
    return client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code


def _set(db: Session, user_id: object, **values: object) -> None:
    db.execute(update(User).where(User.id == user_id).values(**values))
    db.commit()


def test_disabling_a_user_rejects_their_access_token_on_the_next_request(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer()
    assert _me(client, alice.access_token) == 200
    assert client.get("/api/v1/accounts", headers=alice.headers).status_code == 200

    _set(db, alice.user_id, status=UserStatus.DISABLED)  # runbook §5.2 containment step

    for path in ("/api/v1/auth/me", "/api/v1/accounts", "/api/v1/payees"):
        response = client.get(path, headers=alice.headers)
        assert response.status_code == 401, path
        assert response.headers["WWW-Authenticate"] == "Bearer"
        # Indistinguishable from a forged or expired token: no hint that the account exists or why.
        assert response.json()["title"] == "invalid or expired token"

    _set(db, alice.user_id, status=UserStatus.ACTIVE)  # re-enabled: the same token works again
    assert _me(client, alice.access_token) == 200


def test_password_change_revokes_every_outstanding_access_token(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    other_device = login_json(client, alice.email)["access_token"]
    assert _me(client, other_device) == 200

    changed = client.post(
        "/api/v1/auth/password-change",
        headers=alice.headers,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert changed.status_code == 204

    assert _me(client, alice.access_token) == 401  # the token that made the change
    assert _me(client, other_device) == 401  # every other session
    fresh = login_json(client, alice.email, NEW_PASSWORD)["access_token"]
    assert _me(client, fresh) == 200  # a new login carries the new version


def test_another_users_revocation_does_not_affect_this_user(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice, bob = make_customer(), make_customer()
    _set(db, alice.user_id, status=UserStatus.DISABLED)
    assert _me(client, bob.access_token) == 200


def test_token_without_a_version_claim_is_version_zero(
    client: TestClient, make_customer: Callable[..., Actor], settings: Settings, db: Session
) -> None:
    """Tokens issued before the upgrade keep working (no mass logout) until a revocation bumps it."""
    alice = make_customer()
    legacy = encode_token(
        settings,
        subject=alice.user_id,
        token_type="access",
        ttl=timedelta(minutes=5),
        roles=("CUSTOMER",),
        amr=("pwd",),
    )
    assert _me(client, legacy) == 200
    _set(db, alice.user_id, token_version=User.token_version + 1)
    assert _me(client, legacy) == 401


def test_token_for_a_deleted_or_unknown_user_is_rejected(
    client: TestClient, settings: Settings
) -> None:
    import uuid

    ghost = encode_token(
        settings,
        subject=uuid.uuid4(),
        token_type="access",
        ttl=timedelta(minutes=5),
        roles=("ADMIN",),
        amr=("pwd", "otp"),
    )
    assert _me(client, ghost) == 401


def test_requests_racing_a_disable_never_succeed_after_it_commits(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    """Concurrent reads while the account is disabled: once the update has committed, every request
    that starts afterwards is rejected (no cached allow decision anywhere)."""
    alice = make_customer()
    _set(db, alice.user_id, status=UserStatus.DISABLED)
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: _me(client, alice.access_token), range(16)))
    assert codes == [401] * 16
