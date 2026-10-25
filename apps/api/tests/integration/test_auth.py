import base64
import json
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.errors import ConflictError, NotFoundError, UnprocessableError
from pbip_api.core.security import encode_token
from pbip_api.modules.auth.models import RefreshToken, SecurityEvent, SecurityEventType, User
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.models import AuditLog
from pbip_test_support import PASSWORD, Actor, FakeClock, login_json, totp_code
from sqlalchemy import select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration
CSRF = {"X-Requested-With": "pbip-web"}
NEW_PASSWORD = "Correct-Horse-New-9!"  # pragma: allowlist secret
WRONG_PASSWORD = "WrongPassword-123!"  # pragma: allowlist secret


def register(client: TestClient, email: str, password: str = PASSWORD) -> Any:
    return client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Asha Rao"},
    )


def test_register_login_and_me(client: TestClient) -> None:
    assert register(client, "Asha@Example.com").status_code == 201
    token = login_json(client, "asha@example.com")["access_token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "asha@example.com"
    assert me.json()["roles"] == ["CUSTOMER"]


@pytest.mark.parametrize(
    "password", ["short1!", "aaaaaaaaaaaaaaaa", "onlylettersandmore", "asha-password-1234"]
)
def test_weak_passwords_are_rejected(client: TestClient, password: str) -> None:
    assert register(client, "asha@example.com", password).status_code == 422


def test_duplicate_email_is_rejected(client: TestClient) -> None:
    assert register(client, "dup@example.com").status_code == 201
    assert register(client, "DUP@example.com").status_code == 409


def test_unknown_user_and_wrong_password_are_indistinguishable(client: TestClient) -> None:
    register(client, "real@example.com")
    wrong = client.post(
        "/api/v1/auth/login", json={"email": "real@example.com", "password": "Nope-Nope-123!"}
    )
    unknown = client.post(
        "/api/v1/auth/login", json={"email": "ghost@example.com", "password": "Nope-Nope-123!"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["title"] == unknown.json()["title"]
    assert wrong.json()["code"] == unknown.json()["code"]


def test_progressive_lockout_and_recovery(client: TestClient, clock: FakeClock) -> None:
    register(client, "lock@example.com")
    for _ in range(5):
        bad = client.post(
            "/api/v1/auth/login", json={"email": "lock@example.com", "password": "Wrong-Pass-123!"}
        )
        assert bad.status_code == 401
    good_while_locked = client.post(
        "/api/v1/auth/login", json={"email": "lock@example.com", "password": PASSWORD}
    )
    assert good_while_locked.status_code == 423
    bad_while_locked = client.post(
        "/api/v1/auth/login", json={"email": "lock@example.com", "password": "Wrong-Pass-123!"}
    )
    assert bad_while_locked.status_code == 401  # lock state is not revealed to guessers
    clock.advance(61)
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "lock@example.com", "password": PASSWORD}
        ).status_code
        == 200
    )


def test_login_rate_limit_per_ip(client: TestClient, settings: Settings) -> None:
    settings.rate_limit_login_per_minute = 3
    statuses = [
        client.post(
            "/api/v1/auth/login", json={"email": "x@example.com", "password": "Nope-Nope-123!"}
        ).status_code
        for _ in range(5)
    ]
    assert statuses[:3] == [403, 403, 403]
    assert statuses[3:] == [429, 429]


def test_refresh_rotation_and_reuse_detection(client: TestClient) -> None:
    register(client, "rot@example.com")
    login_json(client, "rot@example.com")
    first_cookie = client.cookies.get("pbip_refresh")
    assert first_cookie

    refreshed = client.post("/api/v1/auth/refresh", headers=CSRF)
    assert refreshed.status_code == 200
    second_cookie = client.cookies.get("pbip_refresh")
    assert second_cookie and second_cookie != first_cookie

    # An attacker replays the OLD (already rotated) token: the whole family must die.
    client.cookies.clear()
    replay = client.post(
        "/api/v1/auth/refresh", headers={**CSRF, "Cookie": f"pbip_refresh={first_cookie}"}
    )
    assert replay.status_code == 401
    client.cookies.clear()
    legit_now_dead = client.post(
        "/api/v1/auth/refresh", headers={**CSRF, "Cookie": f"pbip_refresh={second_cookie}"}
    )
    assert legit_now_dead.status_code == 401


def test_refresh_requires_csrf_header_and_trusted_origin(client: TestClient) -> None:
    register(client, "csrf@example.com")
    login_json(client, "csrf@example.com")
    assert client.post("/api/v1/auth/refresh").status_code == 403
    evil = client.post("/api/v1/auth/refresh", headers={**CSRF, "Origin": "https://evil.example"})
    assert evil.status_code == 403
    assert (
        client.post(
            "/api/v1/auth/refresh", headers={**CSRF, "Origin": "http://localhost:5173"}
        ).status_code
        == 200
    )


def test_logout_revokes_the_session(client: TestClient) -> None:
    register(client, "out@example.com")
    login_json(client, "out@example.com")
    cookie = client.cookies.get("pbip_refresh")
    assert client.post("/api/v1/auth/logout", headers=CSRF).status_code == 204
    client.cookies.clear()
    assert (
        client.post(
            "/api/v1/auth/refresh", headers={**CSRF, "Cookie": f"pbip_refresh={cookie}"}
        ).status_code
        == 401
    )


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_forged_and_invalid_access_tokens_are_rejected(
    client: TestClient, settings: Settings
) -> None:
    register(client, "jwt@example.com")
    real = login_json(client, "jwt@example.com")["access_token"]
    user_id = json.loads(base64.urlsafe_b64decode(real.split(".")[1] + "=="))["sub"]
    claims = {
        "sub": user_id,
        "iss": "pbip",
        "aud": "pbip-api",
        "exp": 9999999999,
        "iat": 1,
        "jti": "x",
        "typ": "access",
        "roles": ["ADMIN"],
        "amr": ["pwd", "otp"],
    }
    alg_none = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}."
    tampered = real.rsplit(".", 1)[0] + ".AAAA" + real.rsplit(".", 1)[1][4:]
    expired = encode_token(
        settings,
        subject=__import__("uuid").UUID(user_id),
        token_type="access",
        ttl=timedelta(seconds=-5),
    )
    wrong_type = encode_token(
        settings,
        subject=__import__("uuid").UUID(user_id),
        token_type="mfa_challenge",
        ttl=timedelta(minutes=5),
    )
    for bad in (alg_none, tampered, expired, wrong_type, "garbage"):
        response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {bad}"})
        assert response.status_code == 401, bad[:20]
    assert client.get("/api/v1/auth/me").status_code == 401


def test_mfa_enrolment_login_replay_protection_and_recovery_codes(
    client: TestClient, clock: FakeClock, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer("mfa@example.com")
    enrolled = client.post("/api/v1/auth/mfa/enroll", headers=alice.headers)
    assert enrolled.status_code == 200
    secret = enrolled.json()["secret"]
    assert enrolled.json()["otpauth_uri"].startswith("otpauth://totp/")

    bad_confirm = client.post(
        "/api/v1/auth/mfa/confirm", headers=alice.headers, json={"code": "000000"}
    )
    assert bad_confirm.status_code == 422
    confirm = client.post(
        "/api/v1/auth/mfa/confirm", headers=alice.headers, json={"code": totp_code(secret, clock)}
    )
    assert confirm.status_code == 200
    recovery = confirm.json()["recovery_codes"]
    assert len(recovery) == 8

    challenge = login_json(client, "mfa@example.com")
    assert challenge["mfa_required"] is True and "access_token" not in {
        k for k, v in challenge.items() if v
    }
    # The challenge token must not work as an access token.
    assert (
        client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {challenge['mfa_token']}"}
        ).status_code
        == 401
    )

    clock.advance(30)
    code = totp_code(secret, clock)
    ok = client.post(
        "/api/v1/auth/mfa/verify", json={"mfa_token": challenge["mfa_token"], "code": code}
    )
    assert ok.status_code == 200
    me = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {ok.json()['access_token']}"}
    )
    assert me.json()["mfa_verified_session"] is True

    replay = client.post(
        "/api/v1/auth/mfa/verify", json={"mfa_token": challenge["mfa_token"], "code": code}
    )
    assert replay.status_code == 401  # same time-step code cannot be used twice

    by_recovery = client.post(
        "/api/v1/auth/mfa/verify", json={"mfa_token": challenge["mfa_token"], "code": recovery[0]}
    )
    assert by_recovery.status_code == 200
    again = client.post(
        "/api/v1/auth/mfa/verify", json={"mfa_token": challenge["mfa_token"], "code": recovery[0]}
    )
    assert again.status_code == 401  # recovery codes are single-use


def test_repeated_bad_mfa_codes_lock_the_account(
    client: TestClient, clock: FakeClock, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer("mfalock@example.com")
    secret = client.post("/api/v1/auth/mfa/enroll", headers=alice.headers).json()["secret"]
    client.post(
        "/api/v1/auth/mfa/confirm", headers=alice.headers, json={"code": totp_code(secret, clock)}
    )
    challenge = login_json(client, "mfalock@example.com")["mfa_token"]
    for _ in range(5):
        assert (
            client.post(
                "/api/v1/auth/mfa/verify", json={"mfa_token": challenge, "code": "123456"}
            ).status_code
            == 401
        )
    clock.advance(30)
    good = client.post(
        "/api/v1/auth/mfa/verify", json={"mfa_token": challenge, "code": totp_code(secret, clock)}
    )
    assert good.status_code == 401  # locked: even the right code is refused during the lock


def test_failed_login_does_not_leak_secrets_into_logs(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    client.post(
        "/api/v1/auth/login",
        json={"email": "leak@example.com", "password": "S3cretPassw0rd-Sentinel!"},
    )
    captured = capsys.readouterr().out
    assert "S3cretPassw0rd-Sentinel" not in captured
    assert "leak@example.com" not in captured


def test_password_change_success_and_login_with_new_password(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer("alice.pwd@example.com")
    resp = client.post(
        "/api/v1/auth/password-change",
        headers=alice.headers,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 204
    cookie_header = resp.headers.get("set-cookie", "")
    assert 'pbip_refresh=""' in cookie_header or "Max-Age=0" in cookie_header

    old_login = client.post(
        "/api/v1/auth/login", json={"email": "alice.pwd@example.com", "password": PASSWORD}
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/api/v1/auth/login", json={"email": "alice.pwd@example.com", "password": NEW_PASSWORD}
    )
    assert new_login.status_code == 200
    assert new_login.json()["access_token"]


def test_password_change_revokes_all_sessions_across_families(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer("alice.multisession@example.com")
    client.post(
        "/api/v1/auth/login",
        json={"email": "alice.multisession@example.com", "password": PASSWORD},
    )
    cookie1 = client.cookies.get("pbip_refresh")
    assert cookie1

    client.cookies.clear()
    client.post(
        "/api/v1/auth/login",
        json={"email": "alice.multisession@example.com", "password": PASSWORD},
    )
    cookie2 = client.cookies.get("pbip_refresh")
    assert cookie2 and cookie2 != cookie1

    resp = client.post(
        "/api/v1/auth/password-change",
        headers=alice.headers,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 204

    client.cookies.set("pbip_refresh", cookie1)
    ref1 = client.post("/api/v1/auth/refresh", headers=CSRF)
    assert ref1.status_code == 401

    client.cookies.set("pbip_refresh", cookie2)
    ref2 = client.post("/api/v1/auth/refresh", headers=CSRF)
    assert ref2.status_code == 401

    tokens = (
        db.execute(select(RefreshToken).where(RefreshToken.user_id == alice.user_id))
        .scalars()
        .all()
    )
    assert len(tokens) >= 2
    for tok in tokens:
        assert tok.revoked_at is not None


def test_password_change_wrong_current_password_and_lockout(
    client: TestClient, make_customer: Callable[..., Actor], settings: Settings
) -> None:
    settings.rate_limit_password_change_per_minute = 20
    alice = make_customer("alice.wrongpwd@example.com")
    resp = client.post(
        "/api/v1/auth/password-change",
        headers=alice.headers,
        json={"current_password": WRONG_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 403
    assert resp.json()["title"] == "incorrect current password"
    assert resp.json()["code"] == "invalid_current_password"

    for _ in range(4):
        bad = client.post(
            "/api/v1/auth/password-change",
            headers=alice.headers,
            json={"current_password": WRONG_PASSWORD, "new_password": NEW_PASSWORD},
        )
        assert bad.status_code == 403

    locked = client.post(
        "/api/v1/auth/password-change",
        headers=alice.headers,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert locked.status_code == 423


def test_password_change_rejects_unchanged_password(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer("alice.samepwd@example.com")
    resp = client.post(
        "/api/v1/auth/password-change",
        headers=alice.headers,
        json={"current_password": PASSWORD, "new_password": PASSWORD},
    )
    assert resp.status_code == 422
    assert resp.json()["title"] == "new password must not match the current password"


@pytest.mark.parametrize(
    "bad_password",
    [
        "short1!",
        "aaaaaaaaaaaaaaaa",
        "onlylettersandmore",
        "alice.weak-password-1234!",
        "P@ssw0rd1234!",
        "Summer2025!",
        "123456789012!",
    ],
)
def test_password_change_rejects_policy_failures(
    client: TestClient, make_customer: Callable[..., Actor], bad_password: str
) -> None:
    alice = make_customer("alice.weak@example.com")
    resp = client.post(
        "/api/v1/auth/password-change",
        headers=alice.headers,
        json={"current_password": PASSWORD, "new_password": bad_password},
    )
    assert resp.status_code == 422


def test_password_change_requires_authentication(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/password-change",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 401


def test_password_change_auditing_and_security_events(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer("alice.audit@example.com")
    resp = client.post(
        "/api/v1/auth/password-change",
        headers=alice.headers,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 204

    sec_event = db.execute(
        select(SecurityEvent).where(
            SecurityEvent.user_id == alice.user_id,
            SecurityEvent.event_type == SecurityEventType.PASSWORD_CHANGED,
        )
    ).scalar_one()
    assert sec_event is not None
    assert PASSWORD not in str(sec_event.details)
    assert NEW_PASSWORD not in str(sec_event.details)

    audit = db.execute(
        select(AuditLog).where(
            AuditLog.actor_user_id == alice.user_id,
            AuditLog.action == "PASSWORD_CHANGED",
        )
    ).scalar_one()
    assert audit is not None
    assert PASSWORD not in str(audit.details)
    assert NEW_PASSWORD not in str(audit.details)

    user = db.get(User, alice.user_id)
    assert user is not None and user.password_changed_at is not None


def test_password_change_rate_limited(
    client: TestClient, make_customer: Callable[..., Actor], settings: Settings
) -> None:
    settings.rate_limit_password_change_per_minute = 3
    alice = make_customer("alice.ratelimit@example.com")
    statuses = [
        client.post(
            "/api/v1/auth/password-change",
            headers=alice.headers,
            json={"current_password": WRONG_PASSWORD, "new_password": NEW_PASSWORD},
        ).status_code
        for _ in range(5)
    ]
    assert statuses[:3] == [403, 403, 403]
    assert statuses[3:] == [429, 429]


def test_concurrent_password_change_requests(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer("alice.concur@example.com")
    new_pwd_1 = "Concurrent-Pass-1!"  # pragma: allowlist secret
    new_pwd_2 = "Concurrent-Pass-2!"  # pragma: allowlist secret

    def do_change(new_pwd: str) -> int:
        res = client.post(
            "/api/v1/auth/password-change",
            headers=alice.headers,
            json={"current_password": PASSWORD, "new_password": new_pwd},
        )
        return res.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_change, new_pwd_1)
        f2 = executor.submit(do_change, new_pwd_2)
        statuses = sorted([f1.result(), f2.result()])

    assert statuses == [204, 403]  # the loser's current password is no longer valid


def test_self_registration_disabled(client: TestClient, settings: Settings) -> None:
    settings.allow_self_registration = False
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "disabled@example.com", "password": PASSWORD, "full_name": "Test User"},
    )
    assert resp.status_code == 403


def test_mfa_enrollment_edge_cases(
    client: TestClient, make_customer: Callable[..., Actor], clock: FakeClock
) -> None:
    alice = make_customer("alice.mfaedges@example.com")
    # Confirm without begin enrollment
    bad_confirm = client.post(
        "/api/v1/auth/mfa/confirm", headers=alice.headers, json={"code": "123456"}
    )
    assert bad_confirm.status_code == 409

    # Begin enrollment
    enroll = client.post("/api/v1/auth/mfa/enroll", headers=alice.headers)
    assert enroll.status_code == 200
    secret = enroll.json()["secret"]

    # Confirm with invalid TOTP code
    bad_code_confirm = client.post(
        "/api/v1/auth/mfa/confirm", headers=alice.headers, json={"code": "000000"}
    )
    assert bad_code_confirm.status_code == 422

    # Confirm with valid TOTP code
    good_confirm = client.post(
        "/api/v1/auth/mfa/confirm",
        headers=alice.headers,
        json={"code": totp_code(secret, clock)},
    )
    assert good_confirm.status_code == 200

    # Try to enroll again when already enabled
    again_enroll = client.post("/api/v1/auth/mfa/enroll", headers=alice.headers)
    assert again_enroll.status_code == 409

    # Try to confirm again when already enabled
    again_confirm = client.post(
        "/api/v1/auth/mfa/confirm",
        headers=alice.headers,
        json={"code": totp_code(secret, clock)},
    )
    assert again_confirm.status_code == 409


def test_auth_service_step_up_and_user_lookups(
    db: Session,
    auth_service_factory: Callable[[Session], Any],
    make_customer: Callable[..., Actor],
    clock: FakeClock,
) -> None:
    service = auth_service_factory(db)
    alice = make_customer("alice.stepup@example.com")

    # verify_step_up before MFA is enabled -> ConflictError
    with pytest.raises(ConflictError, match="enable MFA"):
        service.verify_step_up(alice.user_id, "123456", RequestMeta())

    # Enroll and confirm MFA
    enrollment = service.begin_mfa_enrollment(alice.user_id)
    service.confirm_mfa_enrollment(
        alice.user_id, totp_code(enrollment.secret, clock), RequestMeta()
    )

    # Step up with wrong code -> UnprocessableError
    with pytest.raises(UnprocessableError, match="invalid verification code"):
        service.verify_step_up(alice.user_id, "000000", RequestMeta())

    # Step up with correct code -> succeeds
    clock.advance(30)
    service.verify_step_up(alice.user_id, totp_code(enrollment.secret, clock), RequestMeta())

    # Unknown user in get_user -> NotFoundError
    with pytest.raises(NotFoundError, match="user not found"):
        service.get_user(uuid.uuid4())
