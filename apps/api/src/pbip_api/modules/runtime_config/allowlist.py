"""The ONLY settings an ADMIN may change at runtime (ADR-0031).

This is reviewed code, never data: an admin cannot widen it. Each key is a ``core.config.Settings``
field that is a business limit; secrets, authentication, network and fraud-model settings are
deliberately absent. Adding a key also needs a migration widening ``ck_runtime_settings_key_valid``.
"""

from dataclasses import dataclass
from enum import StrEnum


class RuntimeSettingKey(StrEnum):
    DAILY_TRANSFER_LIMIT_MINOR = "daily_transfer_limit_minor"
    MAX_TRANSFER_MINOR = "max_transfer_minor"
    MAX_ACTIVE_SCHEDULED_TRANSFERS_PER_CUSTOMER = "max_active_scheduled_transfers_per_customer"


class SettingUnit(StrEnum):
    MINOR_UNITS = "MINOR_UNITS"  # integer minor units (paise), never a float
    COUNT = "COUNT"


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: RuntimeSettingKey
    label: str
    description: str
    unit: SettingUnit
    min_value: int
    max_value: int

    def allows(self, value: int) -> bool:
        return self.min_value <= value <= self.max_value


ALLOWLIST: dict[RuntimeSettingKey, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec(
            RuntimeSettingKey.DAILY_TRANSFER_LIMIT_MINOR,
            "Daily transfer limit",
            "Most a customer may send per UTC day, transfers and bill payments together.",
            SettingUnit.MINOR_UNITS,
            10_000,
            1_000_000_000,
        ),
        SettingSpec(
            RuntimeSettingKey.MAX_TRANSFER_MINOR,
            "Per-transfer limit",
            "Most a single transfer, scheduled transfer or bill payment may move.",
            SettingUnit.MINOR_UNITS,
            10_000,
            1_000_000_000,
        ),
        SettingSpec(
            RuntimeSettingKey.MAX_ACTIVE_SCHEDULED_TRANSFERS_PER_CUSTOMER,
            "Scheduled transfers per customer",
            "Active or paused standing orders one customer may hold.",
            SettingUnit.COUNT,
            1,
            200,
        ),
    )
}
