import pytest
from pbip_api.core.config import Settings
from pydantic import SecretStr, ValidationError

pytestmark = pytest.mark.unit

GOOD_SECRET = SecretStr("x" * 48)
GOOD_KEY = SecretStr("some-fernet-key")


def production(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "production",
        "jwt_secret": GOOD_SECRET,
        "field_encryption_key": GOOD_KEY,
        "cookie_secure": True,
        "cors_origins": "https://bank.example",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_local_defaults_are_allowed() -> None:
    settings = Settings(app_env="local")
    assert settings.docs_enabled is True
    assert settings.is_deployed is False


def test_production_with_safe_values_boots_and_hides_docs() -> None:
    settings = production()
    assert settings.docs_enabled is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"jwt_secret": SecretStr("local-dev-only-change-me-local-dev-only-change-me")},
        {"jwt_secret": SecretStr("too-short")},
        {"field_encryption_key": SecretStr("")},
        {"cookie_secure": False},
        {"cors_origins": "*"},
    ],
)
def test_production_rejects_unsafe_configuration(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="Unsafe configuration"):
        production(**overrides)


def test_cors_origin_list_is_parsed_and_trimmed() -> None:
    settings = Settings(cors_origins=" http://a.test , http://b.test ,")
    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]


def test_expiry_settings_have_documented_defaults() -> None:
    """ADR-0036: a step-up waits 15 minutes; a key answers for 24 hours."""
    settings = Settings(app_env="local")
    assert settings.step_up_expiry_minutes == 15
    assert settings.idempotency_ttl_hours == 24


@pytest.mark.parametrize(
    "overrides",
    [
        {"step_up_expiry_minutes": 0},
        {"step_up_expiry_minutes": 1441},
        {"idempotency_ttl_hours": 0},
        {"idempotency_purge_batch_size": 0},
        {"step_up_expiry_batch_size": 0},
    ],
)
def test_expiry_settings_are_bounded(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match=next(iter(overrides))):
        Settings(app_env="local", **overrides)  # type: ignore[arg-type]
