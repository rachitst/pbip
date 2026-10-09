from fastapi import APIRouter, Depends, status

from pbip_api.core.errors import UnprocessableError
from pbip_api.modules.auth.dependencies import (
    Principal,
    get_auth_service,
    get_request_meta,
    require_roles,
)
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.schemas import AdminCreateUserRequest, UserOut
from pbip_api.modules.auth.service import AuthService, RequestMeta

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
