from uuid import UUID

from fastapi import APIRouter, Depends, status
from redis import Redis

from pbip_api.core.errors import UnprocessableError
from pbip_api.core.redis import get_redis
from pbip_api.modules.auth.dependencies import (
    Principal,
    get_auth_service,
    get_request_meta,
    require_roles,
)
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.schemas import (
    AdminCreateUserRequest,
    LockoutStatusOut,
    UnlockResultOut,
    UserOut,
)
from pbip_api.modules.auth.service import AuthService, LockoutStatus, RequestMeta

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

admin_gate = require_roles(RoleName.ADMIN, mfa=True)


@router.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user with explicit roles (ADMIN with MFA)",
)
def create_user(
    body: AdminCreateUserRequest,
    admin: Principal = Depends(admin_gate),
    service: AuthService = Depends(get_auth_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> UserOut:
    try:
        roles = [RoleName(name) for name in body.roles]
    except ValueError as exc:
        raise UnprocessableError("unknown role") from exc
    user = service.create_user(
        body.email, body.password, body.full_name, roles, meta, actor_user_id=admin.user_id
    )
    return UserOut(
        id=user.id,
        email=user.email,
        roles=list(user.role_names),
        mfa_enabled=user.mfa_enabled,
        mfa_verified_session=False,
        created_at=user.created_at,
    )


def _to_lockout_out(status_: LockoutStatus) -> LockoutStatusOut:
    return LockoutStatusOut(
        user_id=status_.user_id,
        failed_login_count=status_.failed_login_count,
        locked_until=status_.locked_until,
        is_locked=status_.is_locked,
        last_failure_at=status_.last_failure_at,
    )


@router.get(
    "/users/{user_id}/lockout",
    response_model=LockoutStatusOut,
    summary="Inspect a user's progressive lockout state (ADMIN with MFA)",
)
def get_lockout(
    user_id: UUID,
    _admin: Principal = Depends(admin_gate),
    service: AuthService = Depends(get_auth_service),
) -> LockoutStatusOut:
    return _to_lockout_out(service.get_lockout_status(user_id))


@router.post(
    "/users/{user_id}/unlock",
    response_model=UnlockResultOut,
    summary="Clear a user's lockout and login rate-limit counters (ADMIN with MFA)",
)
def unlock_user(
    user_id: UUID,
    admin: Principal = Depends(admin_gate),
    service: AuthService = Depends(get_auth_service),
    meta: RequestMeta = Depends(get_request_meta),
    redis_client: Redis = Depends(get_redis),
) -> UnlockResultOut:
    result = service.admin_unlock_user(user_id, admin.user_id, meta, redis_client)
    return UnlockResultOut(
        **_to_lockout_out(result.status).model_dump(),
        previous_failed_login_count=result.previous_failed_login_count,
        was_locked=result.was_locked,
        rate_limit_counters_cleared=result.rate_limit_counters_cleared,
    )
