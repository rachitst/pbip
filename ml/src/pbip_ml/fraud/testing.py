"""Test doubles for the fraud contract. Production code must never import this module."""

from pbip_ml.fraud.contract import (
    FraudDecision,
    FraudReason,
    FraudScoreResult,
    ReasonSource,
    TransactionContext,
)


class AllowAllScorer:
    """Always ALLOW. Lets the payments flow be built and tested before the real engine exists."""

    model_version = "test-double-allow-all"

    def score(self, context: TransactionContext) -> FraudScoreResult:
        del context
        return FraudScoreResult(
            risk_score=0,
            decision=FraudDecision.ALLOW,
            reasons=[],
            model_version=self.model_version,
            latency_ms=0.0,
        )


class FixedDecisionScorer:
    """Returns a preconfigured decision. Used to test every branch of the payments flow."""

    model_version = "test-double-fixed"

    def __init__(self, decision: FraudDecision, risk_score: int | None = None) -> None:
        defaults = {
            FraudDecision.ALLOW: 5,
            FraudDecision.STEP_UP: 45,
            FraudDecision.REVIEW: 72,
            FraudDecision.BLOCK: 95,
        }
        self._decision = decision
        self._risk_score = defaults[decision] if risk_score is None else risk_score

    def score(self, context: TransactionContext) -> FraudScoreResult:
        del context
        reasons = (
            []
            if self._decision is FraudDecision.ALLOW
            else [
                FraudReason(
                    code="TEST_FIXED_DECISION",
                    message=f"Test scorer forced {self._decision.value}",
                    source=ReasonSource.RULE,
                )
            ]
        )
        return FraudScoreResult(
            risk_score=self._risk_score,
            decision=self._decision,
            reasons=reasons,
            model_version=self.model_version,
            latency_ms=0.0,
        )
