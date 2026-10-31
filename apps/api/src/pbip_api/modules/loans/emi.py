"""EMI arithmetic in integer paise (TASK-045).

The reducing-balance formula EMI = P r (1+r)^n / ((1+r)^n - 1) is evaluated with exact
``Fraction`` arithmetic and rounded DOWN to whole paise: rounding up would overpay a fraction of a
paisa each month and, on small long loans, finish the loan early with zero instalments (found by
the Hypothesis property test). Each month's interest is the exact balance x r rounded half up;
principal = EMI - interest. On long, high-rate loans the rounded-down EMI can be a paisa below that
month's rounded interest; the excess is not charged that month (no instalment exceeds the EMI) but
carried to the final instalment. The final instalment carries every rounding difference, so the
principal repaid always sums to exactly the amount borrowed and the balance ends at zero.
"""

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True, slots=True)
class Instalment:
    month: int
    payment_minor: int
    principal_minor: int
    interest_minor: int
    balance_after_minor: int


@dataclass(frozen=True, slots=True)
class Schedule:
    emi_minor: int
    total_interest_minor: int
    total_payable_minor: int
    instalments: tuple[Instalment, ...]


def _round_half_up(value: Fraction) -> int:
    return int((value + Fraction(1, 2)).__floor__()) if value >= 0 else -_round_half_up(-value)


def monthly_rate(annual_rate_bps: int) -> Fraction:
    """Basis points per year -> exact monthly rate (e.g. 1050 bps -> 0.105 / 12)."""
    return Fraction(annual_rate_bps, 10_000 * 12)


def emi(principal_minor: int, annual_rate_bps: int, tenure_months: int) -> int:
    if principal_minor <= 0 or tenure_months <= 0 or annual_rate_bps < 0:
        raise ValueError("principal and tenure must be positive and the rate non-negative")
    r = monthly_rate(annual_rate_bps)
    if r == 0:
        return max(principal_minor // tenure_months, 1)
    growth = (1 + r) ** tenure_months
    return max(int((principal_minor * r * growth / (growth - 1)).__floor__()), 1)


def schedule(principal_minor: int, annual_rate_bps: int, tenure_months: int) -> Schedule:
    payment = emi(principal_minor, annual_rate_bps, tenure_months)
    r = monthly_rate(annual_rate_bps)
    balance = principal_minor
    deferred = 0  # interest above the EMI, carried to the final instalment
    rows: list[Instalment] = []
    for month in range(1, tenure_months + 1):
        due = _round_half_up(balance * r)
        if month == tenure_months:
            # the final month absorbs the rounding of every earlier month
            interest, principal = due + deferred, balance
        else:
            interest = min(due, payment)
            deferred += due - interest
            principal = min(payment - interest, balance)
        balance -= principal
        rows.append(Instalment(month, principal + interest, principal, interest, balance))
    total_interest = sum(row.interest_minor for row in rows)
    return Schedule(payment, total_interest, principal_minor + total_interest, tuple(rows))


def max_principal(emi_budget_minor: int, annual_rate_bps: int, tenure_months: int) -> int:
    """The largest loan whose EMI fits ``emi_budget_minor`` (inverse annuity, rounded down)."""
    if emi_budget_minor <= 0:
        return 0
    r = monthly_rate(annual_rate_bps)
    if r == 0:
        candidate = emi_budget_minor * tenure_months
    else:
        growth = (1 + r) ** tenure_months
        candidate = int((emi_budget_minor * (growth - 1) / (r * growth)).__floor__())
    # several amounts round to the same EMI: settle on the largest one that fits the budget
    while candidate > 0 and emi(candidate, annual_rate_bps, tenure_months) > emi_budget_minor:
        candidate -= 1
    while emi(candidate + 1, annual_rate_bps, tenure_months) <= emi_budget_minor:
        candidate += 1
    return int(candidate)
