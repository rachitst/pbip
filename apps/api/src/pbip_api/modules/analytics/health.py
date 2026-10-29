"""Financial health score (0-100) with explainable components (TASK-041).

Points per component: savings rate 30, expense volatility 25, debt (EMI) to income 25, cash
buffer 20. Every component is computed from the account's ledger over the trailing
``HEALTH_WINDOW_MONTHS`` months (see ``repository.get_financial_health_metrics``). A component
that cannot be computed because the history is too thin scores 0 and says so
(``INSUFFICIENT_DATA``) rather than earning default points: a brand-new account is not "healthy",
it is unknown, and the response carries ``insufficient_data`` so clients can show that.
Recommendation tips are never replaced by a generic message; a tip is dropped only when its
component is at its best.
"""

from uuid import UUID

from pbip_api.modules.analytics.repository import HEALTH_WINDOW_MONTHS, HealthMetrics
from pbip_api.modules.analytics.schemas import (
    Assessment,
    FinancialHealthComponents,
    FinancialHealthScoreResponse,
    HealthScoreComponent,
    Rating,
)

SAVINGS_POINTS = 30
VOLATILITY_POINTS = 25
DEBT_POINTS = 25
BUFFER_POINTS = 20
TARGET_SAVINGS_RATE = 0.20
NO_INCOME_TIP = (
    f"No active credit inflows recorded in the last {HEALTH_WINDOW_MONTHS} months. "
    "Set up direct deposits so income and savings can be measured."
)


def _component(
    score: int, max_score: int, metric: float, formatted: str, assessment: Assessment
) -> HealthScoreComponent:
    return HealthScoreComponent(
        score=score,
        max_score=max_score,
        weight_percentage=float(max_score),
        metric_value=round(metric, 4),
        formatted_value=formatted,
        assessment=assessment,
    )


def calculate_savings_component(
    income_minor: int, spend_minor: int
) -> tuple[HealthScoreComponent, list[str]]:
    """Share of income kept: (income - spend) / income; 20 percent or more earns full points."""
    if income_minor <= 0:
        comp = _component(0, SAVINGS_POINTS, 0.0, "no income observed", "INSUFFICIENT_DATA")
        return comp, [NO_INCOME_TIP]
    rate = (income_minor - spend_minor) / income_minor
    if rate >= TARGET_SAVINGS_RATE:
        return _component(SAVINGS_POINTS, SAVINGS_POINTS, rate, f"{rate * 100:.1f}%", "OPTIMAL"), []
    if rate > 0:
        score = round(rate / TARGET_SAVINGS_RATE * SAVINGS_POINTS)
        tip = (
            f"Your savings rate is {rate * 100:.1f}%. Increasing your monthly savings to 20% or "
            "more accelerates wealth accumulation."
        )
        return _component(score, SAVINGS_POINTS, rate, f"{rate * 100:.1f}%", "MODERATE"), [tip]
    tip = (
        "Your expenses exceed your income inflows. Review discretionary categories to prevent "
        "debt accumulation."
    )
    return _component(0, SAVINGS_POINTS, rate, f"{rate * 100:.1f}%", "DEFICIT"), [tip]


def calculate_volatility_component(
    monthly_spend_minor: list[int],
) -> tuple[HealthScoreComponent, list[str]]:
    """Coefficient of variation of monthly spend; needs at least two months with spend."""
    if len(monthly_spend_minor) < 2:
        comp = _component(
            0, VOLATILITY_POINTS, 0.0, "fewer than two months of spending", "INSUFFICIENT_DATA"
        )
        return comp, ["Expense stability needs at least two months of spending history."]
    mean = sum(monthly_spend_minor) / len(monthly_spend_minor)
    variance = sum((x - mean) ** 2 for x in monthly_spend_minor) / len(monthly_spend_minor)
    cv = variance**0.5 / mean
    formatted = f"{cv:.2f} CV"
    if cv <= 0.15:
        return _component(VOLATILITY_POINTS, VOLATILITY_POINTS, cv, formatted, "OPTIMAL"), []
    if cv <= 0.40:
        score = round(VOLATILITY_POINTS - (cv - 0.15) / 0.25 * 15)
        return _component(score, VOLATILITY_POINTS, cv, formatted, "MODERATE"), []
    score = max(round(10 - (cv - 0.40) / 0.60 * 10), 0)
    tip = (
        "High variation in monthly spending detected. A monthly budget helps smooth "
        "unpredictable spikes."
    )
    return _component(score, VOLATILITY_POINTS, cv, formatted, "VOLATILE"), [tip]


def calculate_debt_component(
    income_minor: int, debt_repayments_minor: int
) -> tuple[HealthScoreComponent, list[str]]:
    """Loan and EMI repayments as a share of income; 20 percent or less earns full points."""
    if income_minor <= 0:
        if debt_repayments_minor > 0:
            comp = _component(0, DEBT_POINTS, 1.0, "repayments without income", "CRITICAL")
            return comp, ["Loan repayments exist without recorded income inflows: repayment risk."]
        comp = _component(0, DEBT_POINTS, 0.0, "no income observed", "INSUFFICIENT_DATA")
        return comp, []
    ratio = debt_repayments_minor / income_minor
    formatted = f"{ratio * 100:.1f}%"
    if ratio == 0:
        return _component(DEBT_POINTS, DEBT_POINTS, ratio, formatted, "NO_DEBT"), []
    if ratio <= 0.20:
        return _component(DEBT_POINTS, DEBT_POINTS, ratio, formatted, "OPTIMAL"), []
    if ratio <= 0.40:
        score = round(DEBT_POINTS - (ratio - 0.20) / 0.20 * 15)
        tip = (
            f"Loan repayments take {ratio * 100:.1f}% of your income. Keeping them under 20% "
            "leaves room for savings."
        )
        return _component(score, DEBT_POINTS, ratio, formatted, "MODERATE"), [tip]
    if ratio <= 0.60:
        score = round(10 - (ratio - 0.40) / 0.20 * 10)
        tip = (
            f"Loan repayments consume {ratio * 100:.1f}% of your income. Prioritise prepaying "
            "high-interest loans."
        )
        return _component(score, DEBT_POINTS, ratio, formatted, "BURDENED"), [tip]
    tip = (
        f"Over {ratio * 100:.1f}% of your income goes toward debt servicing. Restructuring "
        "loans can reduce the monthly outflow."
    )
    return _component(0, DEBT_POINTS, ratio, formatted, "CRITICAL"), [tip]


def calculate_buffer_component(
    available_balance_minor: int, average_monthly_spend_minor: int
) -> tuple[HealthScoreComponent, list[str]]:
    """Months of average spend covered by the available balance; six or more earns full points."""
    if average_monthly_spend_minor <= 0:
        comp = _component(0, BUFFER_POINTS, 0.0, "no spending observed", "INSUFFICIENT_DATA")
        return comp, ["A cash buffer can be assessed once there is spending history."]
    if available_balance_minor <= 0:
        comp = _component(0, BUFFER_POINTS, 0.0, "0.0 months", "VULNERABLE")
        tip = (
            "No liquid cash buffer. Aim to accumulate at least one month of living expenses as "
            "a safety net."
        )
        return comp, [tip]
    months = available_balance_minor / average_monthly_spend_minor
    formatted = f"{months:.1f} months"
    if months >= 6.0:
        return _component(BUFFER_POINTS, BUFFER_POINTS, months, formatted, "OPTIMAL"), []
    if months >= 3.0:
        score = round(15 + (months - 3.0) / 3.0 * 5)
        return _component(score, BUFFER_POINTS, months, formatted, "ADEQUATE"), []
    if months >= 1.0:
        score = round(8 + (months - 1.0) / 2.0 * 7)
        tip = (
            f"Your liquid cash buffer covers {months:.1f} months of expenses. Building toward "
            "three to six months provides emergency resilience."
        )
        return _component(score, BUFFER_POINTS, months, formatted, "LOW"), [tip]
    tip = (
        f"Current reserves cover only {months:.1f} months of expenses. Small regular deposits "
        "will build a reliable buffer."
    )
    return _component(max(round(months * 8), 0), BUFFER_POINTS, months, formatted, "CRITICAL"), [
        tip
    ]


def _rating(overall: int) -> Rating:
    if overall >= 80:
        return "EXCELLENT"
    if overall >= 60:
        return "GOOD"
    if overall >= 40:
        return "FAIR"
    return "NEEDS_ATTENTION"


def compute_financial_health_score(
    account_id: UUID, currency: str, available_balance_minor: int, metrics: HealthMetrics
) -> FinancialHealthScoreResponse:
    """Overall score (sum of the component points, 0-100), the components and the tips."""
    savings, savings_tips = calculate_savings_component(metrics.income_minor, metrics.spend_minor)
    volatility, volatility_tips = calculate_volatility_component(metrics.monthly_spend_minor)
    debt, debt_tips = calculate_debt_component(metrics.income_minor, metrics.debt_repayments_minor)
    average_spend = (
        round(sum(metrics.monthly_spend_minor) / len(metrics.monthly_spend_minor))
        if metrics.monthly_spend_minor
        else 0
    )
    buffer, buffer_tips = calculate_buffer_component(available_balance_minor, average_spend)

    components = (savings, volatility, debt, buffer)
    overall = min(max(sum(c.score for c in components), 0), 100)
    tips = savings_tips + volatility_tips + debt_tips + buffer_tips
    if not tips:
        tips = ["Your financial habits are in great shape. Keep up the steady savings rate."]
    return FinancialHealthScoreResponse(
        account_id=account_id,
        currency=currency,
        overall_score=overall,
        rating=_rating(overall),
        insufficient_data=any(c.assessment == "INSUFFICIENT_DATA" for c in components),
        months_observed=metrics.months_observed,
        window_months=HEALTH_WINDOW_MONTHS,
        components=FinancialHealthComponents(
            savings_rate=savings,
            expense_volatility=volatility,
            emi_to_income=debt,
            buffer_months=buffer,
        ),
        recommendations=tips,
    )
