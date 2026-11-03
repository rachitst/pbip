"""Auth edge cases: disabled users, expired or missing refresh cookies and harmless logouts.

Each test pins an observable security property (status code, problem body, persisted session or
counter state), not just the lines it happens to execute.
"""

from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.modules.auth.models import RefreshToken, User, UserStatus
from pbip_test_support import PASSWORD, Actor, FakeClock
from sqlalchemy import select, update
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration
CSRF = {"X-Requested-With": "pbip-web"}
WRONG_PASSWORD = "WrongPassword-123!"  # pragma: allowlist secret
NEW_PASSWORD = "Correct-Horse-New-9!"  # pragma: allowlist secret


def _disable(db: Session, actor: Actor) -> None:
    db.execute(update(User).where(User.id == actor.user_id).values(status=UserStatus.DISABLED))
    db.commit()


def _user(db: Session, actor: Actor) -> User:
    db.expire_all()
    return db.execute(select(User).where(User.id == actor.user_id)).scalar_one()


def _refresh_rows(db: Session, actor: Actor) -> list[RefreshToken]:
    db.expire_all()
    return list(
        db.execute(select(RefreshToken).where(RefreshToken.user_id == actor.user_id)).scalars()
    )


def _login(client: TestClient, email: str, password: str) -> tuple[int, dict[str, object]]:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return response.status_code, response.json()


def test_disabled_user_cannot_sign_in_and_is_indistinguishable_from_a_wrong_password(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    _disable(db, alice)

    right_status, right_body = _login(client, alice.email, PASSWORD)
    wrong_status, wrong_body = _login(client, alice.email, WRONG_PASSWORD)

    assert right_status == wrong_status == 401
    assert right_body.get("title") == wrong_body.get("title")
    assert "access_token" not in right_body
    # A disabled account does not accumulate failures, so it can never be pushed into a lockout.
    user = _user(db, alice)
    assert user.failed_login_count == 0
    assert user.locked_until is None


def test_disabling_a_user_cuts_off_their_live_access_and_refresh_sessions(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    cookie = client.cookies.get("pbip_refresh")
    assert cookie
    assert client.get("/api/v1/auth/me", headers=alice.headers).status_code == 200

    _disable(db, alice)

    assert client.get("/api/v1/auth/me", headers=alice.headers).status_code == 401
    refreshed = client.post("/api/v1/auth/refresh", headers=CSRF)
    assert refreshed.status_code == 401
    assert "access_token" not in refreshed.json()
    # The whole refresh family is revoked, not just left unusable while the user is disabled.
    rows = _refresh_rows(db, alice)
    assert rows
    assert all(row.revoked_at is not None for row in rows)

    # Re-enabling the user does not resurrect the revoked session.
    db.execute(update(User).where(User.id == alice.user_id).values(status=UserStatus.ACTIVE))
    db.commit()
    client.cookies.clear()
    again = client.post(
        "/api/v1/auth/refresh", headers={**CSRF, "Cookie": f"pbip_refresh={cookie}"}
    )
    assert again.status_code == 401


def test_password_change_is_refused_for_a_disabled_user(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    _disable(db, alice)
    before = _user(db, alice).password_hash

    response = client.post(
        "/api/v1/auth/password-change",
        headers=alice.headers,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 401
    assert _user(db, alice).password_hash == before


def test_an_expired_refresh_token_is_rejected_at_the_boundary(
    client: TestClient, clock: FakeClock, settings: Settings, make_customer: Callable[..., Actor]
) -> None:
    make_customer()
    cookie = client.cookies.get("pbip_refresh")
    assert cookie
    ttl = int(timedelta(days=settings.refresh_ttl_days).total_seconds())

    clock.advance(ttl - 1)
    still_valid = client.post("/api/v1/auth/refresh", headers=CSRF)
    assert still_valid.status_code == 200
    rotated = client.cookies.get("pbip_refresh")
    assert rotated and rotated != cookie

    # The rotated token gets a fresh lifetime; once that has fully elapsed it is refused.
    clock.advance(ttl)
    expired = client.post("/api/v1/auth/refresh", headers=CSRF)
    assert expired.status_code == 401
    assert "access_token" not in expired.json()


def test_logout_without_or_with_a_foreign_cookie_does_not_touch_other_sessions(
    client: TestClient, db: Session, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    cookie = client.cookies.get("pbip_refresh")
    assert cookie
    client.cookies.clear()

    assert client.post("/api/v1/auth/logout", headers=CSRF).status_code == 204
    forged = client.post(
        "/api/v1/auth/logout", headers={**CSRF, "Cookie": "pbip_refresh=not-a-real-session-token"}
    )
    assert forged.status_code == 204

    # Alice's real session survives both and still refreshes.
    assert all(row.revoked_at is None for row in _refresh_rows(db, alice))
    refreshed = client.post(
        "/api/v1/auth/refresh", headers={**CSRF, "Cookie": f"pbip_refresh={cookie}"}
    )
    assert refreshed.status_code == 200
