from pathlib import Path

import pytest
from pbip_api.core.config import Settings
from pydantic import SecretStr, ValidationError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_developer_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings reads ``.env`` from the working directory and the process environment. Run every case
    from an empty directory without the LLM variables, so a developer's ``.env`` (for example the
    demo stack's Groq key and model) cannot fill in a field a case deliberately leaves out."""
    monkeypatch.chdir(tmp_path)
    for name in ("LLM_PROVIDER", "GROQ_API_KEY", "GROQ_MODEL", "GROQ_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)


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
        {"llm_provider": "groq", "groq_model": "openai/gpt-oss-20b"},  # no key (ADR-0037)
        {"llm_provider": "groq", "groq_api_key": SecretStr("gsk_" + "k" * 40)},  # no model
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


def test_groq_is_opt_in_and_its_key_never_shows_in_settings_or_errors() -> None:
    assert Settings(app_env="local").llm_provider == "ollama"  # the default sends nothing out
    key = "gsk_" + "s" * 40
    settings = production(
        llm_provider="groq", groq_api_key=SecretStr(key), groq_model="openai/gpt-oss-20b"
    )
    assert key not in repr(settings) and key not in str(settings.model_dump())
    with pytest.raises(ValueError, match="GROQ_MODEL must be set") as caught:
        production(llm_provider="groq", groq_api_key=SecretStr(key))
    assert key not in str(caught.value)


def test_the_groq_routing_deadline_defaults_to_2_5_s_within_bounds() -> None:
    """ADR-0037 addendum (2026-11-08): a deadline, not a hope; nonsense values refuse to boot."""
    assert Settings.model_fields["groq_timeout_seconds"].default == 2.5  # not a local .env value
    assert Settings(groq_timeout_seconds=0.5).groq_timeout_seconds == 0.5
    assert Settings(groq_timeout_seconds=10).groq_timeout_seconds == 10
    for bad in (0, 0.4, -1, 10.5, 30):
        with pytest.raises(ValueError, match="groq_timeout_seconds"):
            Settings(groq_timeout_seconds=bad)


def test_the_per_customer_groq_budget_defaults_to_10_a_minute_within_bounds() -> None:
    """ADR-0037 addendum (2026-11-08, budget): doubled from 5; Groq is the fast path."""
    assert Settings.model_fields["rate_limit_groq_per_minute"].default == 10
    assert Settings(rate_limit_groq_per_minute=1).rate_limit_groq_per_minute == 1
    assert Settings(rate_limit_groq_per_minute=600).rate_limit_groq_per_minute == 600
    for bad in (0, -1, 601):
        with pytest.raises(ValueError, match="rate_limit_groq_per_minute"):
            Settings(rate_limit_groq_per_minute=bad)


def test_an_empty_groq_reasoning_effort_means_the_model_default() -> None:
    assert Settings(groq_reasoning_effort="").groq_reasoning_effort is None  # type: ignore[arg-type]  # env strings
    assert Settings(groq_reasoning_effort="low").groq_reasoning_effort == "low"
    with pytest.raises(ValueError, match="groq_reasoning_effort"):
        Settings(groq_reasoning_effort="max")  # type: ignore[arg-type]  # outside GPT-OSS levels


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
