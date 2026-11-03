"""ADMIN-only runtime configuration (TASK-108, ADR-0031). MFA session required on every route."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.modules.auth.dependencies import Principal, get_request_meta, require_roles
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.runtime_config.allowlist import RuntimeSettingKey
from pbip_api.modules.runtime_config.schemas import (
    RuntimeSettingOut,
    RuntimeSettingsOut,
    RuntimeSettingUpdate,
    setting_out,
)
from pbip_api.modules.runtime_config.service import RuntimeConfigService

router = APIRouter(prefix="/api/v1/admin/settings", tags=["admin"])

settings_admin_gate = require_roles(RoleName.ADMIN, mfa=True)


def get_runtime_config_service(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RuntimeConfigService:
    return RuntimeConfigService(session, settings)


@router.get(
    "",
    response_model=RuntimeSettingsOut,
    summary="List the runtime-configurable settings with default and effective values (ADMIN, MFA)",
)
def list_runtime_settings(
    _admin: Principal = Depends(settings_admin_gate),
    service: RuntimeConfigService = Depends(get_runtime_config_service),
) -> RuntimeSettingsOut:
    return RuntimeSettingsOut(items=[setting_out(view) for view in service.list_settings()])


@router.put(
    "/{key}",
    response_model=RuntimeSettingOut,
    summary="Change an allow-listed runtime setting, with a reason; audited (ADMIN, MFA)",
)
def change_runtime_setting(
    key: RuntimeSettingKey,
    body: RuntimeSettingUpdate,
    admin: Principal = Depends(settings_admin_gate),
    service: RuntimeConfigService = Depends(get_runtime_config_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> RuntimeSettingOut:
    view = service.change_setting(key, body.value, body.reason, admin.user_id, meta)
    return setting_out(view)
