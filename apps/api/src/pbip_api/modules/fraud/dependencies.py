from typing import TYPE_CHECKING

from fastapi import Depends, Request
from pbip_ml.fraud.contract import FraudScorer
from redis import Redis
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.core.geoip import (
    CountryResolver,
    LocationResolver,
    location_resolver_for,
    resolver_for,
)
from pbip_api.core.redis import get_redis
from pbip_api.core.velocity import VelocityCounters
from pbip_api.modules.fraud.baseline import BaselineRulesScorer
from pbip_api.modules.fraud.hybrid import HybridFraudScorer
from pbip_api.modules.fraud.travel import TravelPolicy

if TYPE_CHECKING:  # the model stack loads only when the hybrid flag is on (fraud/hybrid.py)
    from pbip_ml.fraud.hybrid import HybridScorer


def get_location_resolver(settings: Settings = Depends(get_settings)) -> LocationResolver:
    """IP-to-point lookup for IMPOSSIBLE_TRAVEL (ADR-0034), shared by the payment services (this
    payment's point) and the scorer (earlier sign-ins). Tests override this dependency."""
    return location_resolver_for(settings.geoip_city_db_path)


def get_fraud_scorer(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    redis_client: Redis = Depends(get_redis),
    locations: LocationResolver = Depends(get_location_resolver),
) -> FraudScorer:
    """Single seam where the scoring engine is chosen (ADR-0019). Tests override this dependency.

    The models were loaded once when the app was created (``app.state.fraud_engine``); a request
    never reads a model file.
    """
    engine = getattr(request.app.state, "fraud_engine", None)
    return build_fraud_scorer(session, settings, engine, VelocityCounters(redis_client), locations)


def build_fraud_scorer(
    session: Session,
    settings: Settings,
    engine: "HybridScorer | None",
    counters: VelocityCounters | None,
    locations: LocationResolver | None = None,
) -> FraudScorer:
    """The scorer choice itself, shared by requests and the scheduled-transfer worker (ADR-0023),
    so an unattended run is scored by exactly the engine an interactive transfer would get."""
    travel = TravelPolicy.from_settings(
        settings, locations or location_resolver_for(settings.geoip_city_db_path)
    )
    if settings.fraud_scorer == "hybrid":
        return HybridFraudScorer(session, engine, settings.fraud_model_budget_ms, counters, travel)
    return BaselineRulesScorer(session, counters, travel)


def get_country_resolver(settings: Settings = Depends(get_settings)) -> CountryResolver:
    """IP-to-country lookup for the NEW_COUNTRY rule (ADR-0014). Tests override this dependency."""
    return resolver_for(settings.geoip_country_db_path)
