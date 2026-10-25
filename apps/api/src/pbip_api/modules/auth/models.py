"""Identity, credentials, sessions and security events."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk


class RoleName(StrEnum):
    CUSTOMER = "CUSTOMER"
    RELATIONSHIP_MANAGER = "RELATIONSHIP_MANAGER"
    FRAUD_ANALYST = "FRAUD_ANALYST"
    COMPLIANCE_OFFICER = "COMPLIANCE_OFFICER"
    ADMIN = "ADMIN"


STAFF_ROLES = frozenset(
    {
        RoleName.RELATIONSHIP_MANAGER,
        RoleName.FRAUD_ANALYST,
        RoleName.COMPLIANCE_OFFICER,
        RoleName.ADMIN,
    }
)


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class SecurityEventType(StrEnum):
    USER_REGISTERED = "USER_REGISTERED"
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILURE = "LOGIN_FAILURE"
    LOGIN_PASSWORD_OK_MFA_PENDING = "LOGIN_PASSWORD_OK_MFA_PENDING"  # noqa: S105
    LOGIN_BLOCKED_LOCKED = "LOGIN_BLOCKED_LOCKED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    ACCOUNT_UNLOCKED = "ACCOUNT_UNLOCKED"
    MFA_FAILURE = "MFA_FAILURE"
    MFA_ENABLED = "MFA_ENABLED"
    RECOVERY_CODE_USED = "RECOVERY_CODE_USED"
    TOKEN_REFRESHED = "TOKEN_REFRESHED"  # noqa: S105
    TOKEN_REUSE_DETECTED = "TOKEN_REUSE_DETECTED"  # noqa: S105
    LOGOUT = "LOGOUT"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"  # noqa: S105  # pragma: allowlist secret
    STEP_UP_SUCCESS = "STEP_UP_SUCCESS"
    STEP_UP_FAILURE = "STEP_UP_FAILURE"


class Role(Base):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    description: Mapped[str] = mapped_column(String(200), nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
        in_check("status", UserStatus, "status_valid"),
    )

    id: Mapped[UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        str_enum(UserStatus), default=UserStatus.ACTIVE, nullable=False
    )
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_secret_enc: Mapped[str | None] = mapped_column(Text)
    mfa_last_step: Mapped[int | None] = mapped_column(BigInteger)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()

    roles: Mapped[list["UserRole"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(sorted(role.role_name for role in self.roles))


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_name: Mapped[str] = mapped_column(ForeignKey("roles.name"), primary_key=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_family_id", "family_id"),)

    id: Mapped[UUID] = uuid_pk()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[UUID] = mapped_column(nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    amr: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[UUID | None] = mapped_column()
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))


class MfaRecoveryCode(Base):
    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (Index("ix_mfa_recovery_codes_user_id", "user_id"),)

    id: Mapped[UUID] = uuid_pk()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class SecurityEvent(Base):
    """Append-only authentication/security telemetry (also feeds fraud ATO features)."""

    __tablename__ = "security_events"
    __table_args__ = (
        Index("ix_security_events_user_id_created_at", "user_id", "created_at"),
        Index("ix_security_events_event_type_created_at", "event_type", "created_at"),
    )

    id: Mapped[UUID] = uuid_pk()
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    event_type: Mapped[SecurityEventType] = mapped_column(
        str_enum(SecurityEventType, 48), nullable=False
    )
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    request_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
