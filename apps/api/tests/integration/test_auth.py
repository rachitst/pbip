import base64
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.security import encode_token
from pbip_test_support import PASSWORD, Actor, FakeClock, login_json, totp_code

pytestmark = pytest.mark.integration
CSRF = {"X-Requested-With": "pbip-web"}


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
    assert statuses[:3] == [401, 401, 401]
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
