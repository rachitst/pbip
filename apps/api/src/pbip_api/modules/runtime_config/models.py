"""Current runtime overrides of allow-listed settings (ADR-0031).

A row exists only while a setting is overridden. Its history (who, old and new value, reason) is
the hash-chained audit log, not this table.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, utcnow, uuid_pk
from pbip_api.modules.runtime_config.allowlist import RuntimeSettingKey


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"
    __table_args__ = (
        in_check("key", RuntimeSettingKey, "key_valid"),
        CheckConstraint("value >= 0", name="value_non_negative"),
    )

    id: Mapped[UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
