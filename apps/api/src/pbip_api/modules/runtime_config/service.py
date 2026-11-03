"""Runtime configuration use cases and the effective-settings reader (ADR-0031).

``effective_settings`` is what every reader of an allow-listed limit must use (payments, bills):
it reads the overrides in the caller's own transaction on every check, so the API processes and
the Celery worker see a committed change on their next check with no cache to invalidate.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import structlog
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.core.errors import UnprocessableError
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.runtime_config import repository
from pbip_api.modules.runtime_config.allowlist import ALLOWLIST, RuntimeSettingKey, SettingSpec
from pbip_api.modules.runtime_config.models import RuntimeSetting

_logger = structlog.get_logger(__name__)

AUDIT_ACTION = "RUNTIME_SETTING_CHANGED"
AUDIT_RESOURCE = "runtime_setting"


@dataclass(frozen=True, slots=True)
class RuntimeSettingView:
    spec: SettingSpec
    default_value: int
    effective_value: int
    overridden: bool
    updated_at: datetime | None
    updated_by_user_id: UUID | None


def default_value(base: Settings, key: RuntimeSettingKey) -> int:
    """The environment value of an allow-listed field (every allow-listed field is an int)."""
    value = getattr(base, key.value)
    if not isinstance(value, int):
        raise TypeError("allow-listed settings must be integers")
    return value


def _valid_override(row: RuntimeSetting) -> int | None:
    """An override the allow-list accepts, or None. Out-of-list or out-of-bounds rows can only come
    from editing the database by hand: they are ignored, so the environment value applies."""
    spec = next((s for s in ALLOWLIST.values() if s.key.value == row.key), None)
    if spec is None or not spec.allows(row.value):
        _logger.warning("runtime_setting_ignored", setting_key=row.key)
        return None
    return row.value


def effective_settings(session: Session, base: Settings) -> Settings:
    """``base`` with the valid runtime overrides applied; ``base`` itself when there are none."""
    updates: dict[str, int] = {}
    for row in repository.list_overrides(session):
        value = _valid_override(row)
        if value is not None:
            updates[row.key] = value
    return base.model_copy(update=updates) if updates else base


class RuntimeConfigService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.audit = AuditService(session)

    def list_settings(self) -> list[RuntimeSettingView]:
        rows = {row.key: row for row in repository.list_overrides(self.session)}
        return [self._view(spec, rows.get(spec.key.value)) for spec in ALLOWLIST.values()]

    def change_setting(
        self,
        key: RuntimeSettingKey,
        value: int,
        reason: str,
        actor_user_id: UUID,
        meta: RequestMeta,
    ) -> RuntimeSettingView:
        """One use case, one commit: override row and audit row are written together or not at all.

        The audit-chain lock is taken BEFORE the current value is read, so concurrent changes are
        serialised and each audit row's ``old_value`` is exactly the value it replaced.
        """
        spec = ALLOWLIST[key]
        if not spec.allows(value):
            raise UnprocessableError("value is outside the allowed range for this setting")
        self.audit.lock_chain()
        row = repository.get_override(self.session, key.value)
        old_view = self._view(spec, row)
        if value == old_view.effective_value:
            raise UnprocessableError("the setting already has this value")
        if row is None:
            row = RuntimeSetting(key=key.value, value=value, updated_by_user_id=actor_user_id)
            self.session.add(row)
        else:
            row.value = value
            row.updated_by_user_id = actor_user_id
            row.updated_at = utcnow()
        self.session.flush()
        self.audit.record(
            actor_user_id,
            AUDIT_ACTION,
            AUDIT_RESOURCE,
            key.value,
            ip=meta.ip,
            request_id=meta.request_id,
            details={
                "key": key.value,
                "old_value": old_view.effective_value,
                "new_value": value,
                "default_value": old_view.default_value,
                "reason": reason,
            },
        )
        view = self._view(spec, row)
        self.session.commit()
        return view

    def _view(self, spec: SettingSpec, row: RuntimeSetting | None) -> RuntimeSettingView:
        default = default_value(self.settings, spec.key)
        override = _valid_override(row) if row is not None else None
        return RuntimeSettingView(
            spec=spec,
            default_value=default,
            effective_value=default if override is None else override,
            overridden=override is not None,
            updated_at=row.updated_at if row is not None else None,
            updated_by_user_id=row.updated_by_user_id if row is not None else None,
        )
