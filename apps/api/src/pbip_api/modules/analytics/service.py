"""Service layer for analytics: spending breakdown, month-over-month trends, materialisation.

Read path (never writes): a closed month is served from ``monthly_category_spending`` when the
nightly task has stored it; otherwise, and always for the running month, the breakdown is computed
from the ledger entries (ADR-0011). Write path: ``materialize_monthly_spending`` is the Celery
task body that stores closed months and refreshes the most recent one because a transfer can be
reversed weeks after it was posted. The balance forecast (TASK-042/043) is read-only too, except
that the endpoint may add one low-balance notification per account and month.
"""

import calendar
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.errors import NotFoundError, UnprocessableError
from pbip_api.modules.accounts.service import mask_account_number
from pbip_api.modules.analytics import behaviour, projection, repository, unusual
from pbip_api.modules.analytics.health import compute_financial_health_score
from pbip_api.modules.analytics.models import MonthlyCategorySpending
from pbip_api.modules.analytics.policy import verify_account_ownership
from pbip_api.modules.analytics.projection import ModelDay
from pbip_api.modules.analytics.schemas import (
    BalanceForecastPoint,
    BalanceForecastResponse,
    BalanceHistoryPoint,
    BehaviourAnalyticsResponse,
    CategoryShiftItem,
    CategorySpendingItem,
    CustomerSegmentResponse,
    EndOfMonthProjection,
    FinancialHealthScoreResponse,
    ForecastObligation,
    MonthlySpendingSummary,
    SpendingBreakdownResponse,
    SpendingTrendsResponse,
)
from pbip_api.modules.notifications import service as notifications_service
from pbip_api.modules.payments import service as payments_service

_logger = structlog.get_logger(__name__)
MATERIALIZED_MONTHS_BACK = 2
REVERSAL_LOOKBACK = timedelta(
    days=2
)  # overlaps consecutive nightly runs; refreshing twice is harmless


def percentage_change(current: int, previous: int) -> float | None:
    """Relative change in percent; None when there is no previous value to compare against."""
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100, 2)


def is_closed_month(year: int, month: int, now: datetime) -> bool:
    return (year, month) < (now.year, now.month)


def previous_closed_months(now: datetime, count: int) -> list[tuple[int, int]]:
    """The ``count`` calendar months before the running one, most recent first."""
    months: list[tuple[int, int]] = []
    year, month = now.year, now.month
    for _ in range(count):
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        months.append((year, month))
    return months


def _monthly_aggregates(
    session: Session, account_id: UUID, year: int, month: int
) -> list[MonthlyCategorySpending]:
    if is_closed_month(year, month, utcnow()):
        stored = repository.get_preaggregated_monthly_spending(session, account_id, year, month)
        if stored:
            return stored
    return repository.compute_monthly_spending_from_entries(session, account_id, year, month)


def materialize_monthly_spending(
    session: Session,
    periods: list[tuple[int, int]] | None = None,
    now: datetime | None = None,
    reversed_since: datetime | None = None,
) -> int:
    """Store the per-category spend of every customer account for the given months.

    Defaults to the two most recent closed months, plus the posting month of every transfer
    reversed since ``reversed_since`` (default: the last two days), because a reversal changes a
    month's spend without adding an entry to it. Idempotent (rows are upserted under their unique
    key, vanished categories are deleted) and committed once at the end. Returns the number of
    account-months written. A running month may be passed; the read path ignores stored rows for
    it, so this is only useful to tests.
    """
    now = now or utcnow()
    started = time.monotonic()
    if not repository.try_lock_materialization(session):
        _logger.info("spending_materialization_skipped", reason="another run holds the lock")
        session.rollback()
        return 0
    if periods is None:
        periods = previous_closed_months(now, MATERIALIZED_MONTHS_BACK)
    targets: list[tuple[UUID, int, int]] = [
        (account_id, year, month)
        for year, month in periods
        for account_id in repository.accounts_with_debits(session, year, month)
    ]
    since = reversed_since or now - REVERSAL_LOOKBACK
    targets.extend(
        item
        for item in repository.account_months_reversed_since(session, since)
        if item not in targets
    )
    for account_id, year, month in targets:
        records = repository.compute_monthly_spending_from_entries(session, account_id, year, month)
        repository.upsert_monthly_aggregates(session, account_id, year, month, records)
    session.commit()
    _logger.info(
        "spending_materialized",
        account_months=len(targets),
        accounts=len({account_id for account_id, _year, _month in targets}),
        duration_ms=round((time.monotonic() - started) * 1000, 1),
    )
    return len(targets)


def get_spending_breakdown(
    session: Session,
    customer_id: UUID,
    account_id: UUID,
    year: int | None = None,
    month: int | None = None,
) -> SpendingBreakdownResponse:
    """Categorised spend of one owned account in a calendar month (default: the running month)."""
    account = verify_account_ownership(session, customer_id, account_id)
    now = utcnow()
    target_year, target_month = year or now.year, month or now.month

    records = _monthly_aggregates(session, account.id, target_year, target_month)
    total_spend = sum(r.total_amount_minor for r in records)
    categories = [
        CategorySpendingItem(
            category=r.category,
            amount_minor=r.total_amount_minor,
            percentage=round(r.total_amount_minor / total_spend * 100, 2) if total_spend else 0.0,
            transaction_count=r.transaction_count,
        )
        for r in records
    ]
    return SpendingBreakdownResponse(
        account_id=account.id,
        currency=account.currency,
        period=f"{target_year:04d}-{target_month:02d}",
        year=target_year,
        month=target_month,
        total_spend_minor=total_spend,
        transaction_count=sum(r.transaction_count for r in records),
        categories=categories,
    )


def _summary(
    records: list[MonthlyCategorySpending], year: int, month: int, now: datetime
) -> MonthlySpendingSummary:
    """Totals of a month; the burn rate divides by the days elapsed so far for the running month."""
    total = sum(r.total_amount_minor for r in records)
    running = (year, month) == (now.year, now.month)
    days = max(now.day, 1) if running else calendar.monthrange(year, month)[1]
    return MonthlySpendingSummary(
        period=f"{year:04d}-{month:02d}",
        year=year,
        month=month,
        total_spend_minor=total,
        transaction_count=sum(r.transaction_count for r in records),
        daily_burn_rate_minor=total // days,
    )


def get_spending_trends(
    session: Session,
    customer_id: UUID,
    account_id: UUID,
    year: int | None = None,
    month: int | None = None,
) -> SpendingTrendsResponse:
    """Month-over-month comparison of one owned account: totals, burn rate and category shifts."""
    account = verify_account_ownership(session, customer_id, account_id)
    now = utcnow()
    curr_year, curr_month = year or now.year, month or now.month
    prev_year, prev_month = previous_closed_months(
        datetime(curr_year, curr_month, 1, tzinfo=now.tzinfo), 1
    )[0]

    curr_records = _monthly_aggregates(session, account.id, curr_year, curr_month)
    prev_records = _monthly_aggregates(session, account.id, prev_year, prev_month)
    current = _summary(curr_records, curr_year, curr_month, now)
    previous = _summary(prev_records, prev_year, prev_month, now) if prev_records else None

    curr_by_cat = {r.category: r.total_amount_minor for r in curr_records}
    prev_by_cat = {r.category: r.total_amount_minor for r in prev_records}
    shifts = [
        CategoryShiftItem(
            category=category,
            current_amount_minor=curr_by_cat.get(category, 0),
            previous_amount_minor=prev_by_cat.get(category, 0),
            change_minor=curr_by_cat.get(category, 0) - prev_by_cat.get(category, 0),
            change_percentage=percentage_change(
                curr_by_cat.get(category, 0), prev_by_cat.get(category, 0)
            ),
        )
        for category in sorted(curr_by_cat.keys() | prev_by_cat.keys())
    ]
    increasing = sorted(
        (s for s in shifts if s.change_minor > 0), key=lambda s: s.change_minor, reverse=True
    )
    decreasing = sorted((s for s in shifts if s.change_minor < 0), key=lambda s: s.change_minor)

    return SpendingTrendsResponse(
        account_id=account.id,
        currency=account.currency,
        current_month=current,
        previous_month=previous,
        mom_spend_change_percentage=percentage_change(
            current.total_spend_minor, previous.total_spend_minor if previous else 0
        ),
        daily_burn_rate_minor=current.daily_burn_rate_minor,
        burn_rate_change_percentage=percentage_change(
            current.daily_burn_rate_minor, previous.daily_burn_rate_minor if previous else 0
        ),
        top_categories_increasing=increasing[:5],
        top_categories_decreasing=decreasing[:5],
    )


def get_financial_health_score(
    session: Session, customer_id: UUID, account_id: UUID, now: datetime | None = None
) -> FinancialHealthScoreResponse:
    """Explainable health score of one owned account from its trailing ledger history.

    ``now`` is injected by the router from the request clock so month boundaries are testable.
    """
    account = verify_account_ownership(session, customer_id, account_id)
    owned = repository.owned_account_ids(session, customer_id)
    metrics = repository.get_financial_health_metrics(session, account.id, owned, now or utcnow())
    return compute_financial_health_score(
        account.id, account.currency, account.available_balance, metrics
    )


@dataclass(frozen=True)
class CashFlowProfile:
    """Monthly averages over the health window, from the ledger (minor units)."""

    monthly_income_minor: int
    monthly_spend_minor: int
    discretionary_ratio: float  # share of spend on dining, shopping, entertainment and travel


def get_cash_flow_profile(
    session: Session, customer_id: UUID, account_id: UUID, now: datetime
) -> CashFlowProfile:
    """Real income, spend and lifestyle share of one owned account, for recommendations."""
    account = verify_account_ownership(session, customer_id, account_id)
    owned = repository.owned_account_ids(session, customer_id)
    metrics = repository.get_financial_health_metrics(session, account.id, owned, now)
    months = max(metrics.months_observed, 1)
    rfm = repository.extract_customer_rfm_features(session, customer_id, now)
    return CashFlowProfile(
        monthly_income_minor=metrics.income_minor // months,
        monthly_spend_minor=metrics.spend_minor // months,
        discretionary_ratio=rfm.discretionary_ratio if rfm else 0.0,
    )


def get_customer_segment(session: Session, customer_id: UUID) -> CustomerSegmentResponse:
    """The segment the offline job assigned to the authenticated customer; 404 until it has run."""
    record = repository.get_customer_segment(session, customer_id)
    if record is None:
        raise NotFoundError("no segment has been assigned to this customer yet")
    return CustomerSegmentResponse(
        customer_id=record.customer_id,
        segment=record.segment,
        confidence_score=record.confidence_score,
        recency_days=record.recency_days,
        frequency=record.frequency,
        monetary_minor=record.monetary_minor,
        savings_ratio=record.savings_ratio,
        discretionary_ratio=record.discretionary_ratio,
        explanation=record.explanation,
        assigned_at=record.updated_at,
    )


CustomerActivity = repository.CustomerActivity  # public result type of get_customer_activity


def get_customer_activity(
    session: Session, as_of: datetime, window: timedelta, customer_id: UUID | None = None
) -> list[CustomerActivity]:
    """Balances now and at the window start, and last activity, for every customer (one query).

    ``customer_id`` limits it to one customer the caller has already authorised.
    """
    return repository.customer_activity(session, as_of, window, customer_id)


FORECAST_HISTORY_DAYS = 180
FORECAST_HISTORY_SHOWN_DAYS = 30


def _loan_obligations(
    session: Session, customer_id: UUID, instruction: projection.Obligation, until: date
) -> list[projection.Obligation]:
    """The unpaid instalments due by ``until`` of the loan that ``instruction`` repays, with their
    exact amounts (the last EMI differs). An overdue one is dated ``instruction.date`` (today):
    the engine retries arrears every day (ADR-0024)."""
    from pbip_api.modules.loans import service as loans_service  # lazy: loans imports analytics

    if instruction.loan_id is None:
        raise ValueError("not a loan repayment instruction")
    loan = loans_service.get_loan(session, customer_id, instruction.loan_id)
    return [
        projection.Obligation(
            date=max(instalment.due_date, instruction.date),
            amount_minor=instalment.amount_minor,
            kind=projection.ObligationKind.LOAN_EMI,
            scheduled_transfer_id=instruction.scheduled_transfer_id,
            loan_id=instruction.loan_id,
            reference=instruction.reference,
        )
        for instalment in loan.instalments
        if instalment.status != "PAID" and instalment.due_date <= until
    ]


def upcoming_obligations(
    session: Session, customer_id: UUID, account_id: UUID, today: date, until: date
) -> list[projection.Obligation]:
    """Known debits of an owned account from ``today`` to ``until``: every occurrence of the
    customer's ACTIVE standing orders on it, and the instalments of loans it repays.

    A transfer occurrence before today is left out: the engine records it as missed and does not
    pay it (ADR-0023). Paused and cancelled instructions are not obligations.
    """
    occurrences = payments_service.upcoming_account_occurrences(
        session, customer_id, account_id, until
    )
    obligations = [
        projection.Obligation(
            date=o.occurrence_date,
            amount_minor=o.amount_minor,
            kind=projection.ObligationKind.SCHEDULED_TRANSFER,
            scheduled_transfer_id=o.scheduled_transfer_id,
            loan_id=None,
            reference=o.reference,
        )
        for o in occurrences
        if o.loan_id is None and o.occurrence_date >= today
    ]
    loans_seen: set[UUID] = set()
    for o in occurrences:
        if o.loan_id is not None and o.loan_id not in loans_seen:
            loans_seen.add(o.loan_id)
            instruction = projection.Obligation(
                date=today,
                amount_minor=o.amount_minor,
                kind=projection.ObligationKind.LOAN_EMI,
                scheduled_transfer_id=o.scheduled_transfer_id,
                loan_id=o.loan_id,
                reference=o.reference,
            )
            obligations.extend(_loan_obligations(session, customer_id, instruction, until))
    obligations.sort(key=lambda ob: (ob.date, str(ob.scheduled_transfer_id)))
    return obligations


def _model_days(history: list[int], horizon: int, first_day: date) -> tuple[str, list[ModelDay]]:
    from pbip_ml.forecasting.model import forecast_daily_balance  # lazy: numba

    by_level = {
        round(f.confidence_level * 100): f
        for f in forecast_daily_balance(history, horizon, first_day)
    }
    days = [
        ModelDay(
            median=a.median, lower_80=a.lower, upper_80=a.upper, lower_95=b.lower, upper_95=b.upper
        )
        for a, b in zip(by_level[80].points, by_level[95].points, strict=True)
    ]
    return by_level[80].method, days


def _point(day: projection.ProjectedDay) -> BalanceForecastPoint:
    return BalanceForecastPoint(
        date=day.date,
        predicted_minor=day.predicted_minor,
        lower_80_minor=day.lower_80_minor,
        upper_80_minor=day.upper_80_minor,
        lower_95_minor=day.lower_95_minor,
        upper_95_minor=day.upper_95_minor,
        obligations_minor=day.obligations_minor,
    )


def _end_of_month(
    days: list[projection.ProjectedDay], eom: date, overdraft_limit: int
) -> EndOfMonthProjection:
    last = next(d for d in days if d.date == eom)
    threshold = projection.low_balance_threshold(overdraft_limit)
    return EndOfMonthProjection(
        date=eom,
        predicted_minor=last.predicted_minor,
        lower_80_minor=last.lower_80_minor,
        upper_80_minor=last.upper_80_minor,
        lower_95_minor=last.lower_95_minor,
        upper_95_minor=last.upper_95_minor,
        obligations_minor=sum(d.obligations_minor for d in days if d.date <= eom),
        alert_threshold_minor=threshold,
        low_balance=projection.is_low(last, threshold),
    )


def get_balance_forecast(
    session: Session, customer_id: UUID, account_id: UUID, horizon_days: int, now: datetime
) -> BalanceForecastResponse:
    """Daily end-of-day balance projection for an owned account (TASK-042), combining the known
    scheduled obligations with the model forecast of the rest (TASK-043). Predicted, not exact.

    Read-only: the assistant's tool calls this too. The forecast always reaches the month end,
    even when ``horizon_days`` is shorter, so the month-end figure is always there.
    """
    from pbip_ml.forecasting.model import MODEL_VERSION  # lazy: numba

    account = verify_account_ownership(session, customer_id, account_id)
    history = repository.daily_balances(session, account.id, now, FORECAST_HISTORY_DAYS)
    if len(history) < 2:
        raise UnprocessableError("at least two days of account history are needed for a forecast")
    today = now.date()
    history_start = today - timedelta(days=len(history))
    eom = projection.month_end(today)
    model_horizon = max(horizon_days, (eom - today).days + 1)
    until = today + timedelta(days=model_horizon - 1)
    discretionary, paid_in_history = projection.discretionary_history(
        history,
        history_start,
        repository.scheduled_debits_by_day(session, account.id, now, FORECAST_HISTORY_DAYS),
    )
    method, model_days = _model_days(discretionary, model_horizon, today)
    obligations = upcoming_obligations(session, customer_id, account.id, today, until)
    days = projection.project(model_days, today, paid_in_history, obligations)
    shown = history[-FORECAST_HISTORY_SHOWN_DAYS:]
    shown_start = today - timedelta(days=len(shown))
    return BalanceForecastResponse(
        account_id=account.id,
        currency=account.currency,
        method=method,
        model_version=MODEL_VERSION,
        history_days=len(history),
        horizon_days=horizon_days,
        points=[_point(day) for day in days[:horizon_days]],
        history=[
            BalanceHistoryPoint(date=shown_start + timedelta(days=i), balance_minor=balance)
            for i, balance in enumerate(shown)
        ],
        obligations=[
            ForecastObligation(
                date=o.date,
                amount_minor=o.amount_minor,
                kind=o.kind.value,
                scheduled_transfer_id=o.scheduled_transfer_id,
                loan_id=o.loan_id,
                reference=o.reference,
            )
            for o in obligations
        ],
        end_of_month=_end_of_month(days, eom, account.overdraft_limit),
    )


def get_balance_forecast_with_alert(
    session: Session,
    customer_id: UUID,
    user_id: UUID,
    account_id: UUID,
    horizon_days: int,
    now: datetime,
) -> BalanceForecastResponse:
    """The forecast, plus the low month-end balance alert (TASK-043), computed on read.

    When the projected month-end balance is at or below the threshold, the customer gets one bell
    notification for this account and month; later reads in the same month send nothing more.
    Commits only when it adds that notification.
    """
    forecast = get_balance_forecast(session, customer_id, account_id, horizon_days, now)
    eom = forecast.end_of_month
    if eom.low_balance:
        account = verify_account_ownership(session, customer_id, account_id)
        added = notifications_service.notify_projected_low_balance(
            session,
            user_id=user_id,
            account_id=account.id,
            account_masked=mask_account_number(account.account_number),
            month_end=eom.date,
            predicted_minor=eom.predicted_minor,
            currency=account.currency,
            now=now,
        )
        if added:
            session.commit()
            _logger.info("projected_low_balance_alert_sent", month=eom.date.strftime("%Y-%m"))
    return forecast


def get_transaction_volume(
    session: Session, customer_id: UUID, as_of: datetime, window: timedelta
) -> repository.TransactionVolume:
    """Posted inflows and outflows with other parties over ``window`` up to ``as_of``.

    Takes a customer id the caller has already authorised (an RM view, TASK-066); it never reads
    one from the client.
    """
    return repository.transaction_volume(session, customer_id, as_of - window, as_of)


# Public face of the TASK-110 rule for other modules (they use the service, not its internals).
UnusualCategory = unusual.UnusualCategory
UnusualTransaction = unusual.UnusualTransaction
UNUSUAL_BASELINE_MONTHS = unusual.BASELINE_MONTHS
UNUSUAL_CATEGORY_RATIO = unusual.CATEGORY_RATIO
UNUSUAL_TRANSACTION_RATIO = unusual.TRANSACTION_RATIO
UNUSUAL_MIN_EXCESS_MINOR = unusual.MIN_EXCESS_MINOR


@dataclass(frozen=True)
class UnusualExpenses:
    """Unusual spend of one owned account in a calendar month against its own baseline."""

    currency: str
    year: int
    month: int
    baseline_months: list[tuple[int, int]]  # oldest first
    findings: unusual.UnusualFindings


class NotEnoughSpendingHistoryError(UnprocessableError):
    code = "not_enough_spending_history"


def get_unusual_expenses(
    session: Session, customer_id: UUID, account_id: UUID, year: int, month: int
) -> UnusualExpenses:
    """TASK-110: the month's categories and payments well above this customer's usual level.

    The rule and its thresholds are in :mod:`analytics.unusual`. Read-only; one query over the
    period and its three baseline months.
    """
    account = verify_account_ownership(session, customer_id, account_id)
    baseline_months = sorted(
        previous_closed_months(datetime(year, month, 1, tzinfo=UTC), unusual.BASELINE_MONTHS)
    )
    start = repository.month_window(*baseline_months[0])[0]
    period_start, period_end = repository.month_window(year, month)
    owned = repository.owned_account_ids(session, customer_id)
    debits = repository.spend_debits(session, account.id, owned, start, period_end)
    baseline = [d for d in debits if d.posted_at < period_start]
    if unusual.baseline_months_with_spend(baseline) < unusual.MIN_BASELINE_MONTHS:
        raise NotEnoughSpendingHistoryError(
            f"I need spending in at least {unusual.MIN_BASELINE_MONTHS} of the "
            f"{unusual.BASELINE_MONTHS} months before this one to know what is usual for you."
        )
    period = [d for d in debits if d.posted_at >= period_start]
    return UnusualExpenses(
        currency=account.currency,
        year=year,
        month=month,
        baseline_months=baseline_months,
        findings=unusual.find_unusual(period, baseline),
    )


def get_behaviour_analytics(
    session: Session, now: datetime, days: int, segment: str | None
) -> BehaviourAnalyticsResponse:
    """Staff behavioural dashboard (TASK-101): aggregates over every customer, no drill-down."""
    return BehaviourAnalyticsResponse.model_validate(
        behaviour.behaviour_analytics(session, now, days, segment)
    )
