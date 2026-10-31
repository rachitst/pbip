"""Application settings, validated at start-up.

Rules enforced here (see AGENTS.md section 6):
* every secret comes from the environment, never from source code;
* staging/production refuse to boot with development defaults.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
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

    jwt_secret: SecretStr = SecretStr(_DEV_JWT_SECRET)
    jwt_access_ttl_minutes: int = 15
    refresh_ttl_days: int = 7
    # One or more Fernet keys, comma separated, newest first (enables key rotation).
    field_encryption_key: SecretStr = SecretStr("")
    # DB-IP country Lite MMDB (ADR-0014); unset means no geographic fraud signal, never a guess.
    geoip_country_db_path: str | None = None

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

    # Banking limits (INTEGER MINOR UNITS)
    internal_bank_code: str = "PBIP0001"
    max_transfer_minor: int = 20_000_000  # 2,00,000.00 per transfer
    daily_transfer_limit_minor: int = 50_000_000  # 5,00,000.00 per customer per UTC day
    idempotency_ttl_hours: int = 24

    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    cookie_secure: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_deployed(self) -> bool:
        return self.app_env in ("staging", "production")

    @property
    def docs_enabled(self) -> bool:
        return not self.is_deployed

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
