from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from pbip_api.core.validation import Text
from pbip_api.modules.runtime_config.allowlist import RuntimeSettingKey, SettingUnit
from pbip_api.modules.runtime_config.service import RuntimeSettingView

MIN_REASON_CHARS = 10
MAX_REASON_CHARS = 500


class RuntimeSettingOut(BaseModel):
    key: RuntimeSettingKey
    label: str
    description: str
    unit: SettingUnit = Field(description="MINOR_UNITS values are integer minor units (paise)")
    min_value: int
    max_value: int
    default_value: int = Field(description="The value from the deployment's environment")
    effective_value: int = Field(description="The value enforced now")
    overridden: bool
    updated_at: datetime | None
    updated_by_user_id: UUID | None


class RuntimeSettingsOut(BaseModel):
    items: list[RuntimeSettingOut]


class RuntimeSettingUpdate(BaseModel):
    """A new value (checked against the key's allow-listed bounds) and why, for the audit log."""

    model_config = ConfigDict(extra="forbid")

    value: StrictInt = Field(ge=0, le=10**12)
    reason: Annotated[Text, StringConstraints(strip_whitespace=True)] = Field(
        min_length=MIN_REASON_CHARS, max_length=MAX_REASON_CHARS
    )


def setting_out(view: RuntimeSettingView) -> RuntimeSettingOut:
    spec = view.spec
    return RuntimeSettingOut(
        key=spec.key,
        label=spec.label,
        description=spec.description,
        unit=spec.unit,
        min_value=spec.min_value,
        max_value=spec.max_value,
        default_value=view.default_value,
        effective_value=view.effective_value,
        overridden=view.overridden,
        updated_at=view.updated_at,
        updated_by_user_id=view.updated_by_user_id,
    )
