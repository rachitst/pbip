from fastapi import Depends
from pbip_ml.fraud.contract import FraudScorer
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.core.geoip import CountryResolver, resolver_for
from pbip_api.modules.fraud.baseline import BaselineRulesScorer


def get_fraud_scorer(session: Session = Depends(get_session)) -> FraudScorer:
    """Single seam where the scoring engine is chosen. Tests override this dependency."""
    return BaselineRulesScorer(session)


def get_country_resolver(settings: Settings = Depends(get_settings)) -> CountryResolver:
    """IP-to-country lookup for the NEW_COUNTRY rule (ADR-0014). Tests override this dependency."""
    return resolver_for(settings.geoip_country_db_path)
