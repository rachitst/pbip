from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from pbip_api.core.validation import EncodableText, Text


class RegisterRequest(BaseModel):
    email: EmailStr
    password: EncodableText = Field(min_length=1, max_length=256)
    full_name: Text = Field(min_length=2, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: EncodableText = Field(min_length=1, max_length=256)


class MfaVerifyRequest(BaseModel):
    mfa_token: str = Field(min_length=10, max_length=2048)
    code: str = Field(min_length=6, max_length=16)


class MfaConfirmRequest(BaseModel):
    code: str = Field(min_length=6, max_length=8, pattern=r"^[0-9]+$")


class TokenResponse(BaseModel):
    access_token: str | None = None
    token_type: str = "bearer"  # noqa: S105
    expires_in: int | None = None
    mfa_required: bool = False
    mfa_token: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    roles: list[str]
    mfa_enabled: bool
    mfa_verified_session: bool
    created_at: datetime | None = None


class MfaEnrollmentOut(BaseModel):
    secret: str
    otpauth_uri: str


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]


class AdminCreateUserRequest(BaseModel):
    email: EmailStr
    password: EncodableText = Field(min_length=1, max_length=256)
    full_name: Text = Field(min_length=2, max_length=200)
    roles: list[str] = Field(min_length=1)


class PasswordChangeRequest(BaseModel):
    current_password: EncodableText = Field(min_length=1, max_length=256)
    new_password: EncodableText = Field(min_length=1, max_length=256)


class LockoutStatusOut(BaseModel):
    user_id: UUID
    failed_login_count: int
    locked_until: datetime | None
    is_locked: bool
    last_failure_at: datetime | None


class UnlockResultOut(LockoutStatusOut):
    previous_failed_login_count: int
    was_locked: bool
    rate_limit_counters_cleared: int | None = Field(
        description="Redis rate-limit counters removed; null if Redis was unavailable"
    )
