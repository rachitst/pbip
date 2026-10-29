"""Calculation engine for customer Financial Health Score (0-100).

Weights:
- Savings rate: 30%
- Expense volatility: 25%
- EMI / Debt-to-income: 25%
- Buffer months: 20%
"""

from typing import Any

from pbip_api.modules.analytics.schemas import (
    FinancialHealthComponents,
    FinancialHealthScoreResponse,
    HealthScoreComponent,
)


def calculate_savings_component(
    total_credits: int, total_debits: int
) -> tuple[HealthScoreComponent, list[str]]:
    """Compute savings rate component score (weight 30%)."""
    tips: list[str] = []
    if total_credits <= 0:
        comp = HealthScoreComponent(
            score=0,
            max_score=30,
            weight_percentage=30.0,
            metric_value=0.0,
            formatted_value="0.0%",
            assessment="VULNERABLE",
        )
        tips.append("No active credit inflows recorded. Set up direct deposits to build savings.")
        return comp, tips

    net_saved = total_credits - total_debits
    rate = net_saved / total_credits

    if rate >= 0.20:
        score = 30
        assessment = "OPTIMAL"
    elif rate > 0:
        score = round((rate / 0.20) * 30)
        assessment = "MODERATE"
        tips.append(
            f"Your savings rate is {rate * 100:.1f}%. Increasing your monthly savings to >=20% "
            "accelerates wealth accumulation."
        )
    else:
        score = 0
        assessment = "DEFICIT"
        tips.append(
            "Your monthly expenses exceed total income inflows. Review discretionary categories "
            "to prevent debt accumulation."
        )

    comp = HealthScoreComponent(
        score=score,
        max_score=30,
        weight_percentage=30.0,
        metric_value=round(rate, 4),
        formatted_value=f"{rate * 100:.1f}%",
        assessment=assessment,
    )
    return comp, tips


def calculate_volatility_component(
    monthly_debits: list[int],
) -> tuple[HealthScoreComponent, list[str]]:
    """Compute expense stability component score (weight 25%)."""
    tips: list[str] = []
    if len(monthly_debits) < 2:
        comp = HealthScoreComponent(
            score=20,
            max_score=25,
            weight_percentage=25.0,
            metric_value=0.0,
            formatted_value="Baseline (initial period)",
            assessment="STABLE",
        )
        return comp, tips

    mean = sum(monthly_debits) / len(monthly_debits)
    if mean == 0:
        comp = HealthScoreComponent(
            score=25,
            max_score=25,
            weight_percentage=25.0,
            metric_value=0.0,
            formatted_value="0.00 CV",
            assessment="OPTIMAL",
        )
        return comp, tips

    variance = sum((x - mean) ** 2 for x in monthly_debits) / len(monthly_debits)
    std_dev = variance**0.5
    cv = std_dev / mean

    if cv <= 0.15:
        score = 25
        assessment = "OPTIMAL"
    elif cv <= 0.40:
        score = round(25 - ((cv - 0.15) / (0.40 - 0.15)) * 15)
        assessment = "MODERATE"
    else:
        score = max(round(10 - ((cv - 0.40) / 0.60) * 10), 0)
        assessment = "VOLATILE"
        tips.append(
            "High variation in monthly spending detected. Creating a monthly budget helps "
            "smooth unpredictable spikes."
        )

    comp = HealthScoreComponent(
        score=score,
        max_score=25,
        weight_percentage=25.0,
        metric_value=round(cv, 4),
        formatted_value=f"{cv:.2f} CV",
        assessment=assessment,
    )
    return comp, tips


def calculate_emi_component(
    total_credits: int, total_emi_debits: int
) -> tuple[HealthScoreComponent, list[str]]:
    """Compute EMI / Debt-to-income component score (weight 25%)."""
    tips: list[str] = []
    if total_credits <= 0:
        if total_emi_debits > 0:
            comp = HealthScoreComponent(
                score=0,
                max_score=25,
                weight_percentage=25.0,
                metric_value=1.0,
                formatted_value=">100%",
                assessment="CRITICAL",
            )
            tips.append(
                "EMI obligations exist without recorded income inflows. Urgent repayment risk."
            )
            return comp, tips
        comp = HealthScoreComponent(
            score=25,
            max_score=25,
            weight_percentage=25.0,
            metric_value=0.0,
            formatted_value="0.0%",
            assessment="OPTIMAL",
        )
        return comp, tips

    ratio = total_emi_debits / total_credits
    if ratio == 0:
        score = 25
        assessment = "NO_DEBT"
    elif ratio <= 0.20:
        score = 25
        assessment = "OPTIMAL"
    elif ratio <= 0.40:
        score = round(25 - ((ratio - 0.20) / 0.20) * 15)
        assessment = "MODERATE"
        tips.append(
            f"EMI burden is {ratio * 100:.1f}% of income. Keeping debt obligations under 20% "
            "leaves ample cushion for savings."
        )
    elif ratio <= 0.60:
        score = round(10 - ((ratio - 0.40) / 0.20) * 10)
        assessment = "BURDENED"
        tips.append(
            f"Debt commitments consume {ratio * 100:.1f}% of income. Prioritize prepayment "
            "of high-interest loans."
        )
    else:
        score = 0
        assessment = "CRITICAL"
        tips.append(
            f"Over {ratio * 100:.1f}% of monthly income goes toward debt servicing. Restructuring "
            "loans can reduce monthly payments."
        )

    comp = HealthScoreComponent(
        score=score,
        max_score=25,
        weight_percentage=25.0,
        metric_value=round(ratio, 4),
        formatted_value=f"{ratio * 100:.1f}%",
        assessment=assessment,
    )
    return comp, tips


def calculate_buffer_component(
    available_balance: int, avg_monthly_debits: int
) -> tuple[HealthScoreComponent, list[str]]:
    """Compute buffer months component score (weight 20%)."""
    tips: list[str] = []
    if available_balance <= 0:
        comp = HealthScoreComponent(
            score=0,
            max_score=20,
            weight_percentage=20.0,
            metric_value=0.0,
            formatted_value="0.0 months",
            assessment="VULNERABLE",
        )
        tips.append(
            "No liquid cash buffer. Aim to accumulate at least 1 month of living expenses "
            "as a safety net."
        )
        return comp, tips

    if avg_monthly_debits <= 0:
        comp = HealthScoreComponent(
            score=20,
            max_score=20,
            weight_percentage=20.0,
            metric_value=12.0,
            formatted_value=">= 12.0 months",
            assessment="OPTIMAL",
        )
        return comp, tips

    months = available_balance / avg_monthly_debits
    if months >= 6.0:
        score = 20
        assessment = "OPTIMAL"
    elif months >= 3.0:
        score = round(15 + ((months - 3.0) / 3.0) * 5)
        assessment = "ADEQUATE"
    elif months >= 1.0:
        score = round(8 + ((months - 1.0) / 2.0) * 7)
        assessment = "LOW"
        tips.append(
            f"Your liquid cash buffer covers {months:.1f} months of expenses. Building toward "
            "3 to 6 months provides essential emergency resilience."
        )
    else:
        score = max(round(months * 8), 0)
        assessment = "CRITICAL"
        tips.append(
            f"Current reserves cover only {months:.1f} months of expenses. Setting aside small "
            "regular deposits will build a reliable buffer."
        )

    comp = HealthScoreComponent(
        score=score,
        max_score=20,
        weight_percentage=20.0,
        metric_value=round(months, 2),
        formatted_value=f"{months:.1f} months",
        assessment=assessment,
    )
    return comp, tips


def compute_financial_health_score(
    account_id: Any,
    currency: str,
    available_balance: int,
    total_credits: int,
    total_debits: int,
    monthly_debit_totals: list[int],
    total_emi_debits: int,
) -> FinancialHealthScoreResponse:
    """Calculate overall financial health score (0-100) and actionable advice."""
    savings_comp, s_tips = calculate_savings_component(total_credits, total_debits)
    vol_comp, v_tips = calculate_volatility_component(monthly_debit_totals)
    emi_comp, e_tips = calculate_emi_component(total_credits, total_emi_debits)

    avg_debits = (
        round(sum(monthly_debit_totals) / len(monthly_debit_totals))
        if monthly_debit_totals
        else total_debits
    )
    buffer_comp, b_tips = calculate_buffer_component(available_balance, avg_debits)

    raw_sum = savings_comp.score + vol_comp.score + emi_comp.score + buffer_comp.score
    overall = min(max(raw_sum, 0), 100)

    if overall >= 80:
        rating = "EXCELLENT"
        all_tips = [
            "Your financial habits are in great shape. Keep maintaining your steady savings rate!"
        ]
    elif overall >= 60:
        rating = "GOOD"
        all_tips = s_tips + v_tips + e_tips + b_tips
    elif overall >= 40:
        rating = "FAIR"
        all_tips = s_tips + v_tips + e_tips + b_tips
    else:
        rating = "NEEDS_ATTENTION"
        all_tips = s_tips + v_tips + e_tips + b_tips

    if not all_tips:
        all_tips.append("Continue tracking category spending to maintain your healthy score.")

    return FinancialHealthScoreResponse(
        account_id=account_id,
        currency=currency,
        overall_score=overall,
        rating=rating,
        components=FinancialHealthComponents(
            savings_rate=savings_comp,
            expense_volatility=vol_comp,
            emi_to_income=emi_comp,
            buffer_months=buffer_comp,
        ),
        recommendations=all_tips[:4],
    )
