"""Application settings, validated at start-up.

Rules enforced here (see AGENTS.md section 6):
* every secret comes from the environment, never from source code;
* staging/production refuse to boot with development defaults.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_JWT_SECRET = "local-dev-only-change-me-local-dev-only-change-me"  # noqa: S105
_MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "test", "staging", "production"] = "local"
    log_level: str = "INFO"

    # 127.0.0.1: compose publishes on IPv4 only; "localhost" tries ::1 first (stalls on Windows)
    database_url: str = "postgresql+psycopg://pbip:pbip_local_password@127.0.0.1:5432/pbip"
    redis_url: str = "redis://127.0.0.1:6379/0"
    # libpq waits forever by default; a stalled connect then blocks a request or Celery task (and a
    # worker's warm shutdown) indefinitely. libpq treats values below 2 as 2.
    database_connect_timeout_seconds: int = Field(default=5, ge=2, le=60)
    # SQLAlchemy pool per API process (TASK-079). Sync endpoints run on a 40-thread pool, so size +
    # overflow bounds how many requests hold a connection at once; the rest wait up to the timeout.
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    jwt_secret: SecretStr = SecretStr(_DEV_JWT_SECRET)
    jwt_access_ttl_minutes: int = 15
    refresh_ttl_days: int = 7
    # One or more Fernet keys, comma separated, newest first (enables key rotation).
    field_encryption_key: SecretStr = SecretStr("")
    # DB-IP country Lite MMDB (ADR-0014); unset means no geographic fraud signal, never a guess.
    geoip_country_db_path: str | None = None
    # DB-IP City Lite MMDB for IMPOSSIBLE_TRAVEL (ADR-0034); unset means the rule never scores.
    geoip_city_db_path: str | None = None
    # IMPOSSIBLE_TRAVEL fires when the implied speed between the customer's previous located event
    # and this one exceeds the first AND the distance exceeds the second (ADR-0034 justifies both).
    fraud_travel_max_speed_kmh: float = Field(default=900.0, gt=0, le=100_000)
    fraud_travel_min_distance_km: float = Field(default=500.0, gt=0, le=20_000)

    jwt_issuer: str = "pbip"
    jwt_audience: str = "pbip-api"
    mfa_challenge_ttl_minutes: int = 5

    # Argon2id cost. Defaults follow the RFC 9106 low-memory recommendation; tests lower them.
    argon2_time_cost: int = 3
    argon2_memory_kib: int = 65_536
    argon2_parallelism: int = 4

    # Brute-force protection
    login_max_failures: int = 5
    login_lock_base_seconds: int = 60
    rate_limit_login_per_minute: int = 10
    rate_limit_register_per_hour: int = 10
    rate_limit_password_change_per_minute: int = 5
    rate_limit_events_per_minute: int = 120
    rate_limit_assistant_per_minute: int = Field(default=10, ge=1, le=600)
    allow_self_registration: bool = True
    # The readiness probe is public; caching its DB+Redis result bounds the load anyone can push
    # onto the database through it. 0 disables caching.
    health_ready_cache_seconds: float = Field(default=1.0, ge=0, le=30)

    # OpenTelemetry tracing (TASK-085 spike, ADR 0017). Off by default. "none" creates spans
    # (trace ids in logs) without exporting; "console" prints spans to stdout (development);
    # "memory" keeps every span in memory and is for tests only (refused when deployed).
    otel_enabled: bool = False
    otel_service_name: str = Field(default="pbip-api", min_length=1, max_length=64)
    otel_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    otel_exporter: Literal["none", "console", "memory"] = "none"

    # AI assistant (Blueprint section 10). The LLM endpoint is fixed configuration: never a
    # model-chosen or user-supplied URL (SSRF). Scheme, host and port only - no path.
    ollama_base_url: str = Field(
        default="http://localhost:11434", pattern=r"^https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?/?$"
    )
    ollama_model: str = Field(default="qwen2.5:1.5b-instruct", min_length=1, max_length=100)
    # Past this the deterministic rules answer instead, so a slow model never stalls the chat.
    ollama_timeout_seconds: float = Field(default=4.0, gt=0, le=120)
    # How long Ollama keeps the model in memory after a question (Ollama duration syntax).
    ollama_keep_alive: str = Field(default="24h", pattern=r"^-?[0-9]{1,5}[smh]?$")
    assistant_llm_enabled: bool = True
    # Which model routes unclear assistant questions (ADR-0037). "groq" sends the redacted routing
    # question to Groq's hosted API first, then falls back to Ollama, then to the rules. The Groq
    # base URL is a constant in code (SSRF); only the key, model and limits are configuration.
    llm_provider: Literal["ollama", "groq"] = "ollama"
    groq_api_key: SecretStr = SecretStr("")
    # Empty = not chosen. Set from Groq's current model list after the benchmark (ADR-0037).
    groq_model: str = Field(default="", pattern=r"^[A-Za-z0-9._:/-]{0,100}$")
    groq_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    # None = the model's default (sent as nothing). GPT-OSS accepts low|medium|high only.
    groq_reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    # Groq calls per customer per minute; above it the question goes to Ollama, not to Groq.
    rate_limit_groq_per_minute: int = Field(default=5, ge=1, le=600)

    # Fraud scoring (ADR-0019). "rules" = baseline rules only. "hybrid" adds the trained models
    # from FRAUD_MODEL_DIR (operator-controlled; it holds a pickle) with the rules as a floor.
    # Any model problem, or scoring slower than the budget, falls back to the rules.
    fraud_scorer: Literal["rules", "hybrid"] = "rules"
    fraud_model_dir: str | None = Field(default=None, max_length=500)
    # The model step's wait (#362). At most 100 ms: the scoring contract is p95 < 100 ms (AGENTS
    # section 8). Measure a slow machine with scripts/benchmark_fraud_latency.py before raising it.
    fraud_model_budget_ms: int = Field(default=50, ge=5, le=100)
    # MULE_PASS_THROUGH (#144, ADR-0035): inbound transfers from other customers within the window
    # totalling at least the minimum, and an outbound of at least the ratio of them. The ratio is
    # an integer percentage so the boundary is exact (0 < R <= 1). Not admin-overridable (ADR-0031).
    fraud_mule_window_minutes: int = Field(default=60, ge=1, le=1440)
    fraud_mule_min_inbound_minor: int = Field(default=1_000_000, ge=1)  # 10,000.00
    fraud_mule_min_ratio_percent: int = Field(default=80, ge=1, le=100)
    fraud_mule_repeat_episodes: int = Field(default=2, ge=1, le=100)  # earlier episodes, 30 days

    # Banking limits (INTEGER MINOR UNITS)
    internal_bank_code: str = "PBIP0001"
    max_transfer_minor: int = 20_000_000  # 2,00,000.00 per transfer
    daily_transfer_limit_minor: int = 50_000_000  # 5,00,000.00 per customer per UTC day
    # A key replays its answer for this long, then counts as unused and is purged (ADR-0036).
    idempotency_ttl_hours: int = Field(default=24, ge=1, le=720)
    idempotency_purge_batch_size: int = Field(default=1000, ge=1, le=10_000)
    # An unanswered step-up is declined after this many minutes (ADR-0036): long enough to fetch a
    # phone and open an authenticator, short enough that a stale fraud decision is not completed
    # hours later and an abandoned transfer stops counting against the day's limit.
    step_up_expiry_minutes: int = Field(default=15, ge=1, le=1440)
    step_up_expiry_batch_size: int = Field(default=200, ge=1, le=1000)
    scheduled_transfers_batch_size: int = Field(default=100, ge=1, le=1000)
    # Matured fixed deposits paid out per hourly run (ADR-0029).
    fd_maturity_payout_batch_size: int = Field(default=100, ge=1, le=1000)
    # Live (ACTIVE or PAUSED) standing orders one customer may hold (ADR-0023 R6).
    max_active_scheduled_transfers_per_customer: int = Field(default=20, ge=1, le=1000)

    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    cookie_secure: bool = False

    @field_validator("groq_reasoning_effort", mode="before")
    @classmethod
    def _empty_effort_is_default(cls, value: object) -> object:
        """``GROQ_REASONING_EFFORT=`` (as in .env.example and compose) means the model default."""
        return None if value == "" else value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_deployed(self) -> bool:
        return self.app_env in ("staging", "production")

    @property
    def docs_enabled(self) -> bool:
        return not self.is_deployed

    @property
    def fraud_demo_lab_enabled(self) -> bool:
        """The staff fraud demo lab (TASK-081) moves money as demo customers: local/test only."""
        return self.app_env in ("local", "test")

    @field_validator("geoip_country_db_path", "geoip_city_db_path", mode="before")
    @classmethod
    def _blank_geoip_path_means_unset(cls, value: object) -> object:
        """``GEOIP_CITY_DB_PATH=`` (as in .env.example) must mean "no database", the same as unset.

        Environment variables arrive as strings, so an empty assignment would otherwise be ``''``
        rather than ``None`` (found on Windows, where ``dev.py`` exports the .env values)."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _reject_unsafe_deployed_config(self) -> "Settings":
        if not self.is_deployed:
            return self
        problems: list[str] = []
        secret = self.jwt_secret.get_secret_value()
        if secret == _DEV_JWT_SECRET or len(secret) < _MIN_SECRET_LENGTH:
            problems.append("JWT_SECRET must be a unique value of at least 32 characters")
        if not self.field_encryption_key.get_secret_value():
            problems.append("FIELD_ENCRYPTION_KEY must be set")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true")
        if "*" in self.cors_origin_list:
            problems.append("CORS_ORIGINS must not contain a wildcard")
        if self.llm_provider == "groq" and not self.groq_api_key.get_secret_value():
            problems.append("GROQ_API_KEY must be set when LLM_PROVIDER=groq")
        if self.llm_provider == "groq" and not self.groq_model:
            problems.append("GROQ_MODEL must be set when LLM_PROVIDER=groq")
        if self.otel_enabled and self.otel_exporter == "memory":
            problems.append("OTEL_EXPORTER=memory keeps every span in memory (tests only)")
        if problems:
            raise ValueError(
                "Unsafe configuration for " + self.app_env + ": " + "; ".join(problems)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
