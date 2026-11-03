import pytest
from pbip_api.core.config import Settings
from pydantic import SecretStr

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


@pytest.mark.parametrize(
    ("budget", "valid"), [(4, False), (5, True), (50, True), (100, True), (101, False)]
)
def test_fraud_model_budget_is_bounded_by_the_scoring_contract(budget: int, valid: bool) -> None:
    """#362: the model step may wait at most 100 ms (p95 < 100 ms, AGENTS.md section 8)."""
    if valid:
        assert Settings(fraud_model_budget_ms=budget).fraud_model_budget_ms == budget
        return
    with pytest.raises(ValueError, match="fraud_model_budget_ms"):
        Settings(fraud_model_budget_ms=budget)


def test_fraud_model_budget_defaults_to_50_ms() -> None:
    assert Settings.model_fields["fraud_model_budget_ms"].default == 50  # not a local .env value
