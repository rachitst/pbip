"""The scheduled-transfer worker scores with the engine a request would get (ADR-0023, review of #296).

Before the fix the worker always built ``BaselineRulesScorer``, so with ``FRAUD_SCORER=hybrid`` an
unattended run skipped the model that every interactive transfer went through.
"""

from collections.abc import Iterator
from typing import Literal

import pytest
from cryptography.fernet import Fernet
from pbip_api.core.config import Settings
from pbip_api.modules.fraud.baseline import BaselineRulesScorer
from pbip_api.modules.fraud.hybrid import HybridFraudScorer
from pbip_api.modules.payments import scheduled_service
from pydantic import SecretStr
from sqlalchemy.orm import Session

pytestmark = pytest.mark.unit


def _settings(scorer: Literal["rules", "hybrid"]) -> Settings:
    return Settings(
        app_env="local",
        jwt_secret=SecretStr("w" * 48),
        field_encryption_key=SecretStr(Fernet.generate_key().decode()),
        fraud_scorer=scorer,
        fraud_model_dir=None,  # the model cannot load: the hybrid scorer must fail safe to rules
    )


@pytest.fixture(autouse=True)
def _fresh_engine_cache() -> Iterator[None]:
    scheduled_service._worker_fraud_engine.cache_clear()
    yield
    scheduled_service._worker_fraud_engine.cache_clear()


@pytest.mark.parametrize(
    ("flag", "expected"),
    [("hybrid", HybridFraudScorer), ("rules", BaselineRulesScorer)],
)
def test_worker_uses_the_configured_fraud_scorer(
    monkeypatch: pytest.MonkeyPatch, flag: Literal["rules", "hybrid"], expected: type
) -> None:
    settings = _settings(flag)
    monkeypatch.setattr(scheduled_service, "get_settings", lambda: settings)

    service = scheduled_service._build_default_payments_service(Session())

    assert type(service.scorer) is expected
    assert service.counters is not None  # velocity windows, like POST /api/v1/transfers


def test_worker_loads_the_model_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings("hybrid")
    monkeypatch.setattr(scheduled_service, "get_settings", lambda: settings)
    loads: list[Settings] = []
    monkeypatch.setattr(scheduled_service, "load_engine", lambda s: loads.append(s))

    for _ in range(3):
        scheduled_service._build_default_payments_service(Session())

    assert loads == [settings]
