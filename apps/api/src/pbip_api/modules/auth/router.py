from fastapi import APIRouter, Cookie, Depends, Response, status

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.errors import UnauthorizedError
from pbip_api.core.ratelimit import rate_limit
from pbip_api.modules.auth.dependencies import (
    Principal,
    csrf_guard,
    get_auth_service,
    get_current_principal,
    get_request_meta,
)
from pbip_api.modules.auth.schemas import (
    LoginRequest,
    MfaConfirmRequest,
    MfaEnrollmentOut,
    MfaVerifyRequest,
    PasswordChangeRequest,
    RecoveryCodesOut,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from pbip_api.modules.auth.service import AuthService, RequestMeta, SessionTokens

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

REFRESH_COOKIE = "pbip_refresh"
_COOKIE_PATH = "/api/v1/auth"

login_limiter = rate_limit("login", lambda s: s.rate_limit_login_per_minute, 60)
register_limiter = rate_limit("register", lambda s: s.rate_limit_register_per_hour, 3600)
password_change_limiter = rate_limit(
    "password_change", lambda s: s.rate_limit_password_change_per_minute, 60
)


def _set_refresh_cookie(response: Response, tokens: SessionTokens, settings: Settings) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        tokens.refresh_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path=_COOKIE_PATH,
        max_age=settings.refresh_ttl_days * 86400,
    )


def _token_response(tokens: SessionTokens) -> TokenResponse:
    return TokenResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(register_limiter)],
    summary="Self-register a retail customer (disabled in production by configuration)",
)
def register(
    body: RegisterRequest,
    service: AuthService = Depends(get_auth_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> UserOut:
    user = service.register_customer(body.email, body.password, body.full_name, meta)
    return UserOut(
        id=user.id,
        email=user.email,
        roles=list(user.role_names),
        mfa_enabled=user.mfa_enabled,
        mfa_verified_session=False,
        created_at=user.created_at,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(login_limiter)],
    summary="Password login. Returns tokens, or an MFA challenge if MFA is enabled",
)
def login(
    body: LoginRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
    meta: RequestMeta = Depends(get_request_meta),
) -> TokenResponse:
    outcome = service.login(body.email, body.password, meta)
    if outcome.mfa_token is not None:
        return TokenResponse(mfa_required=True, mfa_token=outcome.mfa_token)
    assert outcome.tokens is not None
    _set_refresh_cookie(response, outcome.tokens, settings)
    return _token_response(outcome.tokens)


@router.post(
    "/mfa/verify",
    response_model=TokenResponse,
    dependencies=[Depends(login_limiter)],
    summary="Complete login with a TOTP (or recovery) code",
)
def verify_mfa(
    body: MfaVerifyRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
    meta: RequestMeta = Depends(get_request_meta),
) -> TokenResponse:
    tokens = service.verify_mfa_login(body.mfa_token, body.code, meta)
    _set_refresh_cookie(response, tokens, settings)
    return _token_response(tokens)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    dependencies=[Depends(csrf_guard)],
    summary="Rotate the refresh cookie and get a new access token",
)
def refresh(
    response: Response,
    refresh_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
    meta: RequestMeta = Depends(get_request_meta),
) -> TokenResponse:
    if not refresh_cookie:
        raise UnauthorizedError("invalid session")
    tokens = service.refresh(refresh_cookie, meta)
    _set_refresh_cookie(response, tokens, settings)
    return _token_response(tokens)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(csrf_guard)],
    summary="Revoke the current session family",
)
def logout(
    response: Response,
    refresh_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    service: AuthService = Depends(get_auth_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> None:
    service.logout(refresh_cookie, meta)
    response.delete_cookie(REFRESH_COOKIE, path=_COOKIE_PATH)


@router.get("/me", response_model=UserOut, summary="Who am I?")
def me(
    principal: Principal = Depends(get_current_principal),
    service: AuthService = Depends(get_auth_service),
) -> UserOut:
    user = service.get_user(principal.user_id)
    return UserOut(
        id=user.id,
        email=user.email,
        roles=list(user.role_names),
        mfa_enabled=user.mfa_enabled,
        mfa_verified_session=principal.mfa_verified,
        created_at=user.created_at,
    )


@router.post("/mfa/enroll", response_model=MfaEnrollmentOut, summary="Start TOTP enrolment")
def enroll_mfa(
    principal: Principal = Depends(get_current_principal),
    service: AuthService = Depends(get_auth_service),
) -> MfaEnrollmentOut:
    enrollment = service.begin_mfa_enrollment(principal.user_id)
    return MfaEnrollmentOut(secret=enrollment.secret, otpauth_uri=enrollment.otpauth_uri)


@router.post(
    "/mfa/confirm",
    response_model=RecoveryCodesOut,
    summary="Confirm TOTP enrolment, get recovery codes",
)
def confirm_mfa(
    body: MfaConfirmRequest,
    principal: Principal = Depends(get_current_principal),
    service: AuthService = Depends(get_auth_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> RecoveryCodesOut:
    codes = service.confirm_mfa_enrollment(principal.user_id, body.code, meta)
    return RecoveryCodesOut(recovery_codes=codes)


@router.post(
    "/password-change",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(password_change_limiter)],
    summary="Change user password and invalidate all active session tokens",
)
def change_password(
    body: PasswordChangeRequest,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    service: AuthService = Depends(get_auth_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> None:
    service.change_password(principal.user_id, body.current_password, body.new_password, meta)
    response.delete_cookie(REFRESH_COOKIE, path=_COOKIE_PATH)
