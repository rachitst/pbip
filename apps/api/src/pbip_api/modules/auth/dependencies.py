"""Authentication/authorization dependencies.

``get_current_principal`` is the single authentication gate. The route-policy test fails the
build if any non-public route does not (transitively) depend on a function with this name.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis import Redis
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.crypto import CryptoError, FieldCipher
from pbip_api.core.db import get_session
from pbip_api.core.errors import ForbiddenError, UnauthorizedError
from pbip_api.core.redis import get_redis
from pbip_api.core.security import PasswordService, decode_token
from pbip_api.core.velocity import VelocityCounters
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import AuthService, RequestMeta

_bearer = HTTPBearer(auto_error=False)
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "pbip-web"


class MfaRequiredError(ForbiddenError):
    code = "mfa_required"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    roles: frozenset[str]
    amr: frozenset[str]
    token_id: str

    @property
    def mfa_verified(self) -> bool:
        return "otp" in self.amr

    def has_any_role(self, *roles: RoleName) -> bool:
        return bool(self.roles & {role.value for role in roles})


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError("authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = decode_token(settings, credentials.credentials, "access")
    except UnauthorizedError as exc:
        raise UnauthorizedError(exc.detail, headers={"WWW-Authenticate": "Bearer"}) from exc
    return Principal(claims.subject, claims.roles, claims.amr, claims.token_id)


def require_roles(*roles: RoleName, mfa: bool = False) -> Callable[..., Principal]:
    """Role gate. ``mfa=True`` additionally requires a session that completed the TOTP step."""

    def require_roles_dependency(
        principal: Principal = Depends(get_current_principal),
    ) -> Principal:
        if not principal.has_any_role(*roles):
            raise ForbiddenError("insufficient role")
        if mfa and not principal.mfa_verified:
            raise MfaRequiredError("multi-factor authentication required for this action")
        return principal

    return require_roles_dependency


@lru_cache
def _password_service(time_cost: int, memory_kib: int, parallelism: int) -> PasswordService:
    return PasswordService(time_cost, memory_kib, parallelism)


def get_password_service(settings: Settings = Depends(get_settings)) -> PasswordService:
    return _password_service(
        settings.argon2_time_cost, settings.argon2_memory_kib, settings.argon2_parallelism
    )


def get_field_cipher(settings: Settings = Depends(get_settings)) -> FieldCipher:
    try:
        return FieldCipher(settings.field_encryption_key.get_secret_value())
    except CryptoError as exc:
        raise RuntimeError("FIELD_ENCRYPTION_KEY is not configured (see .env.example)") from exc


def get_request_meta(request: Request) -> RequestMeta:
    request_id = request.scope.get("pbip_request_id")
    return RequestMeta(
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=request_id if isinstance(request_id, str) else None,
    )


def get_clock() -> Callable[[], float]:
    """Wall clock used for TOTP steps and lockouts. Tests override it to be deterministic."""
    return time.time


def get_auth_service(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    passwords: PasswordService = Depends(get_password_service),
    cipher: FieldCipher = Depends(get_field_cipher),
    clock: Callable[[], float] = Depends(get_clock),
    redis_client: Redis = Depends(get_redis),
) -> AuthService:
    return AuthService(session, settings, passwords, cipher, clock, VelocityCounters(redis_client))


def csrf_guard(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Defence in depth for cookie-authenticated endpoints (refresh/logout).

    The refresh cookie is SameSite=Strict; on top of that we require a custom header (which
    cross-site forms cannot send) and, when present, a trusted Origin.
    """
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise ForbiddenError("missing CSRF protection header")
    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.cors_origin_list:
        raise ForbiddenError("origin not allowed")
