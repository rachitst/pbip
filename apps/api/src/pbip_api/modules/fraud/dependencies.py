from fastapi import Depends
from pbip_ml.fraud.contract import FraudScorer
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.fraud.baseline import BaselineRulesScorer


def get_fraud_scorer(session: Session = Depends(get_session)) -> FraudScorer:
    """Single seam where the scoring engine is chosen. Tests override this dependency."""
    return BaselineRulesScorer(session)
