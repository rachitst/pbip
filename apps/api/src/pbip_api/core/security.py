"""Password hashing (Argon2id), JWT access/challenge tokens and opaque-token helpers.

Design rules (docs/PROJECT_BLUEPRINT.md section 6):
* passwords: Argon2id only; unknown users still burn a hash so timing does not reveal them;
* JWTs: HS256 with an explicit algorithm allow-list, required exp/iat/sub/iss/aud/jti and a
  ``typ`` claim so a challenge token can never be used as an access token;
* refresh tokens are OPAQUE random strings stored only as SHA-256 hashes.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from pbip_api.core.config import Settings
from pbip_api.core.errors import UnauthorizedError, UnprocessableError
from pbip_api.core.passwords_blocklist import validate_not_breached_or_common

TokenType = Literal["access", "mfa_challenge"]
_ALGORITHM = "HS256"
_MIN_PASSWORD_LENGTH = 12
_MAX_PASSWORD_LENGTH = 128


class PasswordService:
    def __init__(self, time_cost: int, memory_kib: int, parallelism: int) -> None:
        self._hasher = PasswordHasher(
            time_cost=time_cost, memory_cost=memory_kib, parallelism=parallelism, type=Type.ID
        )
        self._dummy_hash = self._hasher.hash("pbip-dummy-password-used-for-timing-only")

    @classmethod
    def from_settings(cls, settings: Settings) -> "PasswordService":
        return cls(
            settings.argon2_time_cost, settings.argon2_memory_kib, settings.argon2_parallelism
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)

    def burn(self, password: str) -> None:
        """Spend the same CPU as a real verification (used when the user does not exist)."""
        self.verify(self._dummy_hash, password)


def validate_password_strength(password: str, email: str) -> None:
    local_part = email.split("@", 1)[0].lower()
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise UnprocessableError(f"password must be at least {_MIN_PASSWORD_LENGTH} characters")
    if len(password) > _MAX_PASSWORD_LENGTH:
        raise UnprocessableError(f"password must be at most {_MAX_PASSWORD_LENGTH} characters")
    if len(set(password)) < 5:
        raise UnprocessableError("password is too repetitive")
    if len(local_part) >= 4 and local_part in password.lower():
        raise UnprocessableError("password must not contain your email name")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9\W_]", password):
        raise UnprocessableError("password must mix letters with digits or symbols")
    validate_not_breached_or_common(password)


@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: UUID
    token_type: str
    roles: frozenset[str]
    amr: frozenset[str]
    token_id: str
    expires_at: datetime


def encode_token(
    settings: Settings,
    *,
    subject: UUID,
    token_type: TokenType,
    ttl: timedelta,
    roles: tuple[str, ...] = (),
    amr: tuple[str, ...] = (),
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(subject),
        "iat": now,
        "exp": now + ttl,
        "jti": uuid4().hex,
        "typ": token_type,
        "roles": list(roles),
        "amr": list(amr),
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=_ALGORITHM)


def decode_token(settings: Settings, token: str, expected_type: TokenType) -> TokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[_ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud", "jti"]},
        )
        if payload.get("typ") != expected_type:
            raise UnauthorizedError("invalid token type")
        return TokenClaims(
            subject=UUID(payload["sub"]),
            token_type=expected_type,
            roles=frozenset(payload.get("roles", [])),
            amr=frozenset(payload.get("amr", [])),
            token_id=payload["jti"],
            expires_at=datetime.fromtimestamp(payload["exp"], UTC),
        )
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise UnauthorizedError("invalid or expired token") from exc


def new_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def hash_opaque_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
