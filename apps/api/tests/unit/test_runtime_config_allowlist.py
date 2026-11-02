"""ADR-0031 guards: the runtime allow-list stays small, bounded and free of security settings, and
no code reads an overridable limit straight from ``Settings`` (it must use the effective value)."""

import re
from pathlib import Path

import pytest
from annotated_types import Ge, Le
from pbip_api.core.config import Settings
from pbip_api.modules.runtime_config.allowlist import ALLOWLIST, RuntimeSettingKey
from pydantic import SecretStr

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src" / "pbip_api"
# Where an allow-listed field may legitimately be named: its definition and the module that applies
# the overrides. Every other reader goes through ``runtime_config.effective_settings``.
ALLOWED_READERS = {SRC / "core" / "config.py", *(SRC / "modules" / "runtime_config").glob("*.py")}
SECURITY_WORDS = ("secret", "key", "jwt", "argon2", "login", "rate_limit", "mfa", "ttl", "cors",
                  "cookie", "url", "registration", "fraud", "otel", "path")  # fmt: skip


def test_the_allow_list_is_exactly_the_reviewed_business_limits() -> None:
    assert set(ALLOWLIST) == set(RuntimeSettingKey)
    assert {key.value for key in RuntimeSettingKey} == {
        "daily_transfer_limit_minor",
        "max_transfer_minor",
        "max_active_scheduled_transfers_per_customer",
    }


@pytest.mark.parametrize("key", list(RuntimeSettingKey))
def test_each_entry_is_an_int_setting_with_a_sane_bounded_range(key: RuntimeSettingKey) -> None:
    spec = ALLOWLIST[key]
    field = Settings.model_fields[key.value]
    assert field.annotation is int
    assert 0 < spec.min_value <= spec.max_value
    default = field.default
    assert isinstance(default, int) and spec.allows(default), "env default must be allowed"
    for meta in field.metadata:  # runtime bounds never exceed the static Settings bounds
        if isinstance(meta, Ge):
            assert spec.min_value >= int(meta.ge)
        if isinstance(meta, Le):
            assert spec.max_value <= int(meta.le)


def test_no_secret_or_security_setting_is_runtime_configurable() -> None:
    for key in RuntimeSettingKey:
        assert not any(word in key.value for word in SECURITY_WORDS), key
        assert Settings.model_fields[key.value].annotation is not SecretStr
    secrets = [n for n, f in Settings.model_fields.items() if f.annotation is SecretStr]
    assert secrets and not set(secrets) & {k.value for k in RuntimeSettingKey}


def test_no_module_reads_an_overridable_limit_directly_from_settings() -> None:
    pattern = re.compile(r"\bsettings\.(" + "|".join(k.value for k in RuntimeSettingKey) + r")\b")
    offenders = [
        f"{path.relative_to(SRC)}: {match.group(0)}"
        for path in SRC.rglob("*.py")
        if path not in ALLOWED_READERS
        for match in pattern.finditer(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"read the effective value (runtime_config.effective_settings): {offenders}"
    )
