"""Authentication use cases. Every state-changing path commits exactly once at its end.

Failure paths that must persist state (failed-login counters, lockouts, token-family
revocation) commit BEFORE raising, otherwise the rollback would erase the evidence.
"""

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pyotp
import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings
from pbip_api.core.crypto import FieldCipher
from pbip_api.core.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    UnprocessableError,
)
from pbip_api.core.metrics import LOGIN_FAILURES_TOTAL
from pbip_api.core.security import (
    PasswordService,
    decode_token,
    encode_token,
    hash_opaque_token,
    new_opaque_token,
    validate_password_strength,
)
from pbip_api.modules.accounts.models import AccountType
from pbip_api.modules.accounts.service import open_account
from pbip_api.modules.auth.models import (
    MfaRecoveryCode,
    RefreshToken,
    RoleName,
    SecurityEvent,
    SecurityEventType,
    User,
    UserRole,
    UserStatus,
)
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.customers.models import Customer

_logger = structlog.get_logger(__name__)
_TOTP_STEP_SECONDS = 30
_RECOVERY_CODE_COUNT = 8
_MAX_LOCK_SECONDS = 3600
INVALID_CREDENTIALS = "invalid email or password"


class AccountLockedError(DomainError):
    status_code = 423
    code = "account_locked"


class InvalidCurrentPasswordError(ForbiddenError):
    """403, not 401: the caller IS authenticated. A 401 would make clients treat it as an
    expired access token, silently refresh and replay - counting each attempt twice."""

    code = "invalid_current_password"  # pragma: allowlist secret


@dataclass(frozen=True, slots=True)
class RequestMeta:
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionTokens:
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_at: datetime


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    tokens: SessionTokens | None = None
    mfa_token: str | None = None


@dataclass(frozen=True, slots=True)
class MfaEnrollment:
    secret: str
    otpauth_uri: str


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AuthService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        passwords: PasswordService,
        cipher: FieldCipher,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session = session
        self.settings = settings
        self.passwords = passwords
        self.cipher = cipher
        self.clock = clock
        self.audit = AuditService(session)
        self._last_refresh_row_id: UUID | None = None

    # ------------------------------------------------------------------ helpers
    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def _event(
        self,
        event_type: SecurityEventType,
        user_id: UUID | None,
        meta: RequestMeta,
        **details: Any,
    ) -> None:
        self.session.add(
            SecurityEvent(
                user_id=user_id,
                event_type=event_type,
                ip=meta.ip,
                user_agent=(meta.user_agent or "")[:255] or None,
                request_id=meta.request_id,
                details=details,
            )
        )

    def _user_by_email(self, email: str) -> User | None:
        return self.session.execute(
            select(User).where(User.email == normalize_email(email))
        ).scalar_one_or_none()

    def _get_user(self, user_id: UUID) -> User:
        user = self.session.get(User, user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            raise UnauthorizedError("invalid or expired token")
        return user

    def _is_locked(self, user: User) -> bool:
        return user.locked_until is not None and user.locked_until > self._now()

    def _register_failure(self, user: User, meta: RequestMeta, reason: str) -> None:
        """Count a failed credential attempt; apply progressive lockout. Caller commits."""
        user.failed_login_count += 1
        LOGIN_FAILURES_TOTAL.labels(reason).inc()
        self._event(SecurityEventType.LOGIN_FAILURE, user.id, meta, reason=reason)
        over = user.failed_login_count - self.settings.login_max_failures
        if over >= 0:
            seconds = min(
                self.settings.login_lock_base_seconds * (2 ** min(over, 10)), _MAX_LOCK_SECONDS
            )
            user.locked_until = self._now() + timedelta(seconds=seconds)
            self._event(
                SecurityEventType.ACCOUNT_LOCKED, user.id, meta, lock_seconds=seconds, reason=reason
            )

    def _reset_failures(self, user: User) -> None:
        user.failed_login_count = 0
        user.locked_until = None

    # ------------------------------------------------------------ registration
    def create_user(
        self,
        email: str,
        password: str,
        full_name: str,
        roles: list[RoleName],
        meta: RequestMeta,
        actor_user_id: UUID | None = None,
    ) -> User:
        email = normalize_email(email)
        validate_password_strength(password, email)
        if self._user_by_email(email) is not None:
            raise ConflictError("an account with this email already exists")
        user = User(
            id=uuid4(),
            email=email,
            password_hash=self.passwords.hash(password),
            password_changed_at=self._now(),
        )
        user.roles = [UserRole(user_id=user.id, role_name=role.value) for role in roles]
        self.session.add(user)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("an account with this email already exists") from exc
        if RoleName.CUSTOMER in roles:
            customer = Customer(id=uuid4(), user_id=user.id, full_name=full_name.strip())
            self.session.add(customer)
            self.session.flush()
            open_account(self.session, customer.id, AccountType.SAVINGS)
        self._event(
            SecurityEventType.USER_REGISTERED, user.id, meta, roles=[r.value for r in roles]
        )
        self.audit.record(
            actor_user_id or user.id,
            "USER_CREATED",
            "user",
            str(user.id),
            ip=meta.ip,
            request_id=meta.request_id,
            details={"roles": [r.value for r in roles]},
        )
        self.session.commit()
        return user

    def register_customer(
        self, email: str, password: str, full_name: str, meta: RequestMeta
    ) -> User:
        if not self.settings.allow_self_registration:
            raise ForbiddenError("self-registration is disabled")
        return self.create_user(email, password, full_name, [RoleName.CUSTOMER], meta)

    # ------------------------------------------------------------------- login
    def login(self, email: str, password: str, meta: RequestMeta) -> LoginOutcome:
        user = self._user_by_email(email)
        if user is None:
            self.passwords.burn(password)
            LOGIN_FAILURES_TOTAL.labels("unknown_user").inc()
            self._event(SecurityEventType.LOGIN_FAILURE, None, meta, reason="unknown_user")
            self.session.commit()
            raise UnauthorizedError(INVALID_CREDENTIALS)

        password_ok = self.passwords.verify(user.password_hash, password)
        if self._is_locked(user):
            self._event(SecurityEventType.LOGIN_BLOCKED_LOCKED, user.id, meta)
            self.session.commit()
            if password_ok:  # only someone holding the right password learns about the lock
                raise AccountLockedError("account temporarily locked, try again later")
            raise UnauthorizedError(INVALID_CREDENTIALS)

        if not password_ok or user.status != UserStatus.ACTIVE:
            if user.status == UserStatus.ACTIVE:
                self._register_failure(user, meta, "bad_password")
            self.session.commit()
            raise UnauthorizedError(INVALID_CREDENTIALS)

        self._reset_failures(user)
        if self.passwords.needs_rehash(user.password_hash):
            user.password_hash = self.passwords.hash(password)
        if user.mfa_enabled:
            token = encode_token(
                self.settings,
                subject=user.id,
                token_type="mfa_challenge",  # noqa: S106
                ttl=timedelta(minutes=self.settings.mfa_challenge_ttl_minutes),
            )
            self._event(SecurityEventType.LOGIN_PASSWORD_OK_MFA_PENDING, user.id, meta)
            self.session.commit()
            return LoginOutcome(mfa_token=token)

        tokens = self._issue_session(user, ("pwd",), meta)
        user.last_login_at = self._now()
        self._event(SecurityEventType.LOGIN_SUCCESS, user.id, meta, amr=["pwd"])
        self.session.commit()
        return LoginOutcome(tokens=tokens)

    def verify_mfa_login(self, mfa_token: str, code: str, meta: RequestMeta) -> SessionTokens:
        claims = decode_token(self.settings, mfa_token, "mfa_challenge")
        user = self._get_user(claims.subject)
        if self._is_locked(user):
            self.session.commit()
            raise UnauthorizedError("invalid verification code")
        used_recovery = False
        ok = self._verify_totp(user, code)
        if not ok:
            ok = used_recovery = self._consume_recovery_code(user, code)
        if not ok:
            self._event(SecurityEventType.MFA_FAILURE, user.id, meta)
            self._register_failure(user, meta, "bad_mfa_code")
            self.session.commit()
            raise UnauthorizedError("invalid verification code")
        if used_recovery:
            self._event(SecurityEventType.RECOVERY_CODE_USED, user.id, meta)
        self._reset_failures(user)
        tokens = self._issue_session(user, ("pwd", "otp"), meta)
        user.last_login_at = self._now()
        self._event(SecurityEventType.LOGIN_SUCCESS, user.id, meta, amr=["pwd", "otp"])
        self.session.commit()
        return tokens

    # ---------------------------------------------------------------- sessions
    def _issue_session(
        self,
        user: User,
        amr: tuple[str, ...],
        meta: RequestMeta,
        family_id: UUID | None = None,
    ) -> SessionTokens:
        ttl = timedelta(minutes=self.settings.jwt_access_ttl_minutes)
        access = encode_token(
            self.settings,
            subject=user.id,
            token_type="access",  # noqa: S106
            ttl=ttl,
            roles=user.role_names,
            amr=amr,
        )
        raw = new_opaque_token()
        expires_at = self._now() + timedelta(days=self.settings.refresh_ttl_days)
        row = RefreshToken(
            id=uuid4(),
            user_id=user.id,
            family_id=family_id or uuid4(),
            token_hash=hash_opaque_token(raw),
            amr=",".join(amr),
            expires_at=expires_at,
            ip=meta.ip,
            user_agent=(meta.user_agent or "")[:255] or None,
        )
        self.session.add(row)
        self.session.flush()
        self._last_refresh_row_id = row.id
        return SessionTokens(access, int(ttl.total_seconds()), raw, expires_at)

    def _revoke_family(self, family_id: UUID) -> None:
        self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=self._now())
        )

    def _revoke_all_user_sessions(self, user_id: UUID) -> None:
        self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=self._now())
        )

    def change_password(
        self,
        user_id: UUID,
        current_password: str,
        new_password: str,
        meta: RequestMeta,
    ) -> None:
        user = self.session.execute(
            select(User).where(User.id == user_id).with_for_update()
        ).scalar_one_or_none()
        if user is None or user.status != UserStatus.ACTIVE:
            raise UnauthorizedError("invalid or expired token")

        if self._is_locked(user):
            self._event(SecurityEventType.LOGIN_BLOCKED_LOCKED, user.id, meta)
            self.session.commit()
            raise AccountLockedError("account temporarily locked, try again later")

        if not self.passwords.verify(user.password_hash, current_password):
            self._register_failure(user, meta, "bad_password")
            self.session.commit()
            raise InvalidCurrentPasswordError("incorrect current password")

        if current_password == new_password or self.passwords.verify(
            user.password_hash, new_password
        ):
            raise UnprocessableError("new password must not match the current password")

        validate_password_strength(new_password, user.email)

        user.password_hash = self.passwords.hash(new_password)
        user.password_changed_at = self._now()
        self._reset_failures(user)

        self._revoke_all_user_sessions(user.id)
        self._event(SecurityEventType.PASSWORD_CHANGED, user.id, meta)
        self.audit.record(
            user.id,
            "PASSWORD_CHANGED",
            "user",
            str(user.id),
            ip=meta.ip,
            request_id=meta.request_id,
            details={},
        )
        self.session.commit()

    def refresh(self, raw_token: str, meta: RequestMeta) -> SessionTokens:
        row = self.session.execute(
            select(RefreshToken)
            .where(RefreshToken.token_hash == hash_opaque_token(raw_token))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if row is None or row.revoked_at is not None:
            raise UnauthorizedError("invalid session")
        if row.used_at is not None:
            # A rotated token was presented again: assume theft, kill the whole family.
            self._revoke_family(row.family_id)
            self._event(SecurityEventType.TOKEN_REUSE_DETECTED, row.user_id, meta)
            self.session.commit()
            raise UnauthorizedError("invalid session")
        if row.expires_at <= self._now():
            raise UnauthorizedError("session expired")
        user = self.session.get(User, row.user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            self._revoke_family(row.family_id)
            self.session.commit()
            raise UnauthorizedError("invalid session")
        row.used_at = self._now()
        tokens = self._issue_session(user, tuple(row.amr.split(",")), meta, family_id=row.family_id)
        row.replaced_by_id = self._last_refresh_row_id
        self._event(SecurityEventType.TOKEN_REFRESHED, user.id, meta)
        self.session.commit()
        return tokens

    def logout(self, raw_token: str | None, meta: RequestMeta) -> None:
        if not raw_token:
            return
        row = self.session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_opaque_token(raw_token))
        ).scalar_one_or_none()
        if row is not None:
            self._revoke_family(row.family_id)
            self._event(SecurityEventType.LOGOUT, row.user_id, meta)
            self.session.commit()

    # --------------------------------------------------------------------- MFA
    def _verify_totp(self, user: User, code: str) -> bool:
        """Validate a TOTP code in a +/-1 step window; each time-step may be used only once."""
        if not user.mfa_secret_enc or not code.isascii() or not code.isdigit():
            return False
        totp = pyotp.TOTP(self.cipher.decrypt(user.mfa_secret_enc))
        current_step = int(self.clock() // _TOTP_STEP_SECONDS)
        for step in (current_step - 1, current_step, current_step + 1):
            expected = totp.at(step * _TOTP_STEP_SECONDS)
            if hmac.compare_digest(expected, code):
                if user.mfa_last_step is not None and step <= user.mfa_last_step:
                    return False  # replay of an already-used code
                user.mfa_last_step = step
                return True
        return False

    @staticmethod
    def _normalise_recovery(code: str) -> str:
        return code.replace("-", "").strip().lower()

    def _consume_recovery_code(self, user: User, code: str) -> bool:
        digest = hashlib.sha256(self._normalise_recovery(code).encode()).hexdigest()
        row = self.session.execute(
            select(MfaRecoveryCode)
            .where(
                MfaRecoveryCode.user_id == user.id,
                MfaRecoveryCode.code_hash == digest,
                MfaRecoveryCode.used_at.is_(None),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            return False
        row.used_at = self._now()
        return True

    def begin_mfa_enrollment(self, user_id: UUID) -> MfaEnrollment:
        user = self._get_user(user_id)
        if user.mfa_enabled:
            raise ConflictError("MFA is already enabled")
        secret = pyotp.random_base32()
        user.mfa_secret_enc = self.cipher.encrypt(secret)
        self.session.commit()
        uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="PBIP")
        return MfaEnrollment(secret=secret, otpauth_uri=uri)

    def confirm_mfa_enrollment(self, user_id: UUID, code: str, meta: RequestMeta) -> list[str]:
        user = self._get_user(user_id)
        if user.mfa_enabled:
            raise ConflictError("MFA is already enabled")
        if not user.mfa_secret_enc:
            raise ConflictError("start MFA enrollment first")
        if not self._verify_totp(user, code):
            raise UnprocessableError("invalid verification code")
        user.mfa_enabled = True
        codes = [
            f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(_RECOVERY_CODE_COUNT)
        ]
        for plain in codes:
            digest = hashlib.sha256(self._normalise_recovery(plain).encode()).hexdigest()
            self.session.add(MfaRecoveryCode(id=uuid4(), user_id=user.id, code_hash=digest))
        self._event(SecurityEventType.MFA_ENABLED, user.id, meta)
        self.audit.record(
            user.id, "MFA_ENABLED", "user", str(user.id), ip=meta.ip, request_id=meta.request_id
        )
        self.session.commit()
        return codes

    def verify_step_up(self, user_id: UUID, code: str, meta: RequestMeta) -> None:
        """Second-factor check for a sensitive action (e.g. a flagged transfer)."""
        user = self._get_user(user_id)
        if not user.mfa_enabled:
            raise ConflictError("enable MFA to complete this verification")
        if self._is_locked(user):
            raise UnauthorizedError("invalid verification code")
        if not self._verify_totp(user, code):
            self._event(SecurityEventType.STEP_UP_FAILURE, user.id, meta)
            self._register_failure(user, meta, "bad_step_up_code")
            self.session.commit()
            raise UnprocessableError("invalid verification code")
        self._event(SecurityEventType.STEP_UP_SUCCESS, user.id, meta)
        # no commit here: the caller commits together with the business change

    # ----------------------------------------------------------------- profile
    def get_user(self, user_id: UUID) -> User:
        user = self.session.get(User, user_id)
        if user is None:
            raise NotFoundError("user not found")
        return user
