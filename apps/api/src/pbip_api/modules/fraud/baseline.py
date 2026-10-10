"""Baseline, fully explainable rules scorer (deterministic stop-gap until the ML engine ships).

Implements the ``FraudScorer`` contract. Every point of score is attributed to a named rule with
a human-readable message, so an analyst can always see WHY a transfer was flagged. The downstream
hybrid engine (rules + LightGBM + Isolation Forest + SHAP) wraps this behind the same
interface; these rules become its deterministic layer.
"""

import time
from datetime import timedelta

from pbip_ml.fraud.contract import (
    FraudDecision,
    FraudReason,
    FraudScoreResult,
    ReasonSource,
    TransactionContext,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.money import format_minor
from pbip_api.modules.ledger.models import Transaction, TransactionStatus, TransactionType
from pbip_api.modules.payees.models import Payee

MODEL_VERSION = "baseline-rules-0.1"

NEW_PAYEE_WINDOW = timedelta(hours=24)
NEW_PAYEE_LARGE_MINOR = 2_500_000  # 25,000.00
LARGE_AMOUNT_MINOR = 10_000_000  # 1,00,000.00
SPIKE_MULTIPLIER = 5.0
SPIKE_MIN_HISTORY = 3
VELOCITY_WINDOW = timedelta(minutes=10)
VELOCITY_COUNT = 4

STEP_UP_AT = 30
REVIEW_AT = 60
BLOCK_AT = 85


def decide(score: int) -> FraudDecision:
    if score >= BLOCK_AT:
        return FraudDecision.BLOCK
    if score >= REVIEW_AT:
        return FraudDecision.REVIEW
    if score >= STEP_UP_AT:
        return FraudDecision.STEP_UP
    return FraudDecision.ALLOW


class BaselineRulesScorer:
    model_version = MODEL_VERSION

    def __init__(self, session: Session) -> None:
        self.session = session

    def score(self, context: TransactionContext) -> FraudScoreResult:
        started = time.perf_counter()
        now = utcnow()
        reasons: list[FraudReason] = []
        score = 0

        payee_age_minutes: int | None = None
        if context.payee_id is not None:
            created = self.session.execute(
                select(Payee.created_at).where(Payee.id == context.payee_id)
            ).scalar_one_or_none()
            if created is not None:
                payee_age_minutes = int((now - created).total_seconds() // 60)

        history_avg, history_count = self.session.execute(
            select(func.avg(Transaction.amount), func.count()).where(
                Transaction.debit_account_id == context.account_id,
                Transaction.type == TransactionType.TRANSFER,
                Transaction.status == TransactionStatus.POSTED,
                Transaction.created_at >= now - timedelta(days=90),
            )
        ).one()
        average = float(history_avg) if history_avg is not None else 0.0

        recent_count = self.session.execute(
            select(func.count()).where(
                Transaction.debit_account_id == context.account_id,
                Transaction.type == TransactionType.TRANSFER,
                Transaction.id != context.transaction_id,
                Transaction.created_at >= now - VELOCITY_WINDOW,
            )
        ).scalar_one()

        amount_text = format_minor(context.amount_minor, context.currency)
        is_new_payee = payee_age_minutes is not None and (
            timedelta(minutes=payee_age_minutes) < NEW_PAYEE_WINDOW
        )
        if is_new_payee and context.amount_minor >= NEW_PAYEE_LARGE_MINOR:
            score += 45
            reasons.append(
                FraudReason(
                    code="NEW_PAYEE_LARGE_AMOUNT",
                    message=(
                        f"Payee was added {payee_age_minutes} minutes ago "
                        f"and {amount_text} is a large amount"
                    ),
                    source=ReasonSource.RULE,
                    contribution=45,
                )
            )
        elif is_new_payee:
            score += 15
            reasons.append(
                FraudReason(
                    code="NEW_PAYEE",
                    message=f"Payee was added {payee_age_minutes} minutes ago",
                    source=ReasonSource.RULE,
                    contribution=15,
                )
            )
        if history_count >= SPIKE_MIN_HISTORY and average > 0:
            ratio = context.amount_minor / average
            if ratio >= SPIKE_MULTIPLIER:
                score += 35
                reasons.append(
                    FraudReason(
                        code="AMOUNT_SPIKE",
                        message=f"Amount is {ratio:.1f}x this account's usual transfer",
                        source=ReasonSource.RULE,
                        contribution=35,
                    )
                )
        if recent_count + 1 >= VELOCITY_COUNT:
            score += 30
            reasons.append(
                FraudReason(
                    code="VELOCITY_BURST",
                    message=f"{recent_count + 1} transfers from this account within 10 minutes",
                    source=ReasonSource.RULE,
                    contribution=30,
                )
            )
        if context.amount_minor >= LARGE_AMOUNT_MINOR:
            score += 20
            reasons.append(
                FraudReason(
                    code="LARGE_AMOUNT",
                    message=f"{amount_text} exceeds the large-transfer threshold",
                    source=ReasonSource.RULE,
                    contribution=20,
                )
            )

        score = min(score, 100)
        return FraudScoreResult(
            risk_score=score,
            decision=decide(score),
            reasons=reasons,
            model_version=MODEL_VERSION,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            features={
                "amount_minor": context.amount_minor,
                "payee_age_minutes": payee_age_minutes,
                "avg_outgoing_90d_minor": round(average, 2),
                "outgoing_count_90d": int(history_count),
                "transfers_last_10m": int(recent_count),
            },
        )
