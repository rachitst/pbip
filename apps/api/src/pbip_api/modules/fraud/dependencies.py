from fastapi import Depends, Request
from pbip_ml.fraud.contract import FraudScorer
from redis import Redis
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.core.geoip import CountryResolver, resolver_for
from pbip_api.core.redis import get_redis
from pbip_api.core.velocity import VelocityCounters
from pbip_api.modules.fraud.baseline import BaselineRulesScorer
from pbip_api.modules.fraud.hybrid import HybridFraudScorer


def get_fraud_scorer(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    redis_client: Redis = Depends(get_redis),
) -> FraudScorer:
    """Single seam where the scoring engine is chosen (ADR-0019). Tests override this dependency.

    The models were loaded once when the app was created (``app.state.fraud_engine``); a request
    never reads a model file.
    """
    counters = VelocityCounters(redis_client)
    if settings.fraud_scorer == "hybrid":
        engine = getattr(request.app.state, "fraud_engine", None)
        return HybridFraudScorer(session, engine, settings.fraud_model_budget_ms, counters)
    return BaselineRulesScorer(session, counters)


def get_country_resolver(settings: Settings = Depends(get_settings)) -> CountryResolver:
    """IP-to-country lookup for the NEW_COUNTRY rule (ADR-0014). Tests override this dependency."""
    return resolver_for(settings.geoip_country_db_path)
