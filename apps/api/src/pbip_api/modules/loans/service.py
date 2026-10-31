"""Loan catalogue, EMI calculation and eligibility (TASK-045, TASK-046).

Eligibility uses the customer's own ledger figures: average monthly income (analytics cash flow)
and the share of it already going to loan repayments (health score). The rules, in order:
1. A lockout from repeated failed sign-ins in the last 90 days refuses the check (account risk).
2. Debt-to-income (existing repayments + the new EMI) above 50% refuses the loan.
3. Otherwise the largest affordable amount per tenor keeps total repayments within 50% of income.
Every result explains itself with the figures it used.
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.errors import NotFoundError, UnprocessableError
from pbip_api.core.money import format_minor
from pbip_api.modules.accounts import service as accounts_service
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.auth.service import recent_lockouts
from pbip_api.modules.loans import emi as emi_math
from pbip_api.modules.loans.catalogue import PRODUCTS, LoanProduct, get_product
from pbip_api.modules.loans.schemas import (
    EligibilityOut,
    EmiOut,
    InstalmentOut,
    LoanProductOut,
    TenorOption,
)

MAX_DTI = 0.50
LOCKOUT_LOOKBACK = timedelta(days=90)


def list_products() -> list[LoanProductOut]:
    return [
        LoanProductOut(
            id=p.id,
            loan_type=p.loan_type.value,
            name=p.name,
            annual_rate_bps=p.annual_rate_bps,
            min_amount_minor=p.min_amount_minor,
            max_amount_minor=p.max_amount_minor,
            tenures_months=list(p.tenures_months),
        )
        for p in PRODUCTS
    ]


def _product(product_id: UUID, amount_minor: int, tenure_months: int) -> LoanProduct:
    product = get_product(product_id)
    if product is None:
        raise NotFoundError("loan product not found")
    if tenure_months not in product.tenures_months:
        raise UnprocessableError("tenure is not offered for this product")
    if not product.min_amount_minor <= amount_minor <= product.max_amount_minor:
        raise UnprocessableError("amount is outside this product's range")
    return product


def calculate_emi(product_id: UUID, principal_minor: int, tenure_months: int) -> EmiOut:
    product = _product(product_id, principal_minor, tenure_months)
    plan = emi_math.schedule(principal_minor, product.annual_rate_bps, tenure_months)
    return EmiOut(
        product_id=product.id,
        annual_rate_bps=product.annual_rate_bps,
        principal_minor=principal_minor,
        tenure_months=tenure_months,
        emi_minor=plan.emi_minor,
        total_interest_minor=plan.total_interest_minor,
        total_payable_minor=plan.total_payable_minor,
        schedule=[
            InstalmentOut(
                month=i.month,
                payment_minor=i.payment_minor,
                principal_minor=i.principal_minor,
                interest_minor=i.interest_minor,
                balance_after_minor=i.balance_after_minor,
            )
            for i in plan.instalments
        ],
    )


def _tenor_options(product: LoanProduct, budget_minor: int) -> list[TenorOption]:
    options = []
    for months in product.tenures_months:
        amount = min(emi_math.max_principal(budget_minor, product.annual_rate_bps, months),
                     product.max_amount_minor)  # fmt: skip
        if amount >= product.min_amount_minor:
            at_max = emi_math.emi(amount, product.annual_rate_bps, months)
            options.append(
                TenorOption(tenure_months=months, max_amount_minor=amount, emi_at_max_minor=at_max)
            )
    return options


def _income_and_debt(session: Session, customer_id: UUID, now: datetime) -> tuple[int, int]:
    accounts = accounts_service.list_accounts(session, customer_id)
    if not accounts:
        return 0, 0
    primary = accounts[0]
    cash = analytics_service.get_cash_flow_profile(session, customer_id, primary.id, now)
    health = analytics_service.get_financial_health_score(session, customer_id, primary.id, now)
    income = cash.monthly_income_minor
    existing = round(health.components.emi_to_income.metric_value * income)
    return income, existing


def check_eligibility(
    session: Session,
    *,
    customer_id: UUID,
    user_id: UUID,
    product_id: UUID,
    amount_minor: int,
    tenure_months: int,
    now: datetime | None = None,
) -> EligibilityOut:
    now = now or utcnow()
    product = _product(product_id, amount_minor, tenure_months)
    income, existing = _income_and_debt(session, customer_id, now)
    proposed = emi_math.emi(amount_minor, product.annual_rate_bps, tenure_months)
    dti_existing = round(existing / income, 4) if income else 0.0
    dti_with_loan = round((existing + proposed) / income, 4) if income else 1.0
    budget = max(int(income * MAX_DTI) - existing, 0)
    options = _tenor_options(product, budget)
    at_tenure = next((o.max_amount_minor for o in options if o.tenure_months == tenure_months), 0)

    reasons: list[str] = []
    eligible = True
    lockouts = recent_lockouts(session, user_id, now - LOCKOUT_LOOKBACK)
    if lockouts:
        eligible = False
        reasons.append(
            f"Your account was locked {lockouts} time(s) in the last 90 days after repeated "
            "failed sign-ins; loan checks resume 90 days after the last lockout."
        )
    if income == 0:
        eligible = False
        reasons.append("We could not find regular income in your account history.")
    elif dti_with_loan > MAX_DTI:
        eligible = False
        if dti_existing > MAX_DTI:
            reasons.append(
                f"Loan repayments already take {dti_existing:.0%} of your income of "
                f"{format_minor(income, 'INR')} a month, above the 50% limit."
            )
        else:
            reasons.append(
                f"With an EMI of {format_minor(proposed, 'INR')}, repayments would take "
                f"{dti_with_loan:.0%} of your income of {format_minor(income, 'INR')}, above the "
                "50% limit."
            )
    if eligible:
        reasons.append(
            f"An EMI of {format_minor(proposed, 'INR')} brings repayments to {dti_with_loan:.0%} "
            f"of your income of {format_minor(income, 'INR')}, within the 50% limit."
        )
    if at_tenure and amount_minor > at_tenure:
        reasons.append(
            f"Over {tenure_months} months you could borrow up to {format_minor(at_tenure, 'INR')}."
        )
    return EligibilityOut(
        status="ELIGIBLE" if eligible else "NOT_ELIGIBLE",
        product_id=product.id,
        requested_amount_minor=amount_minor,
        tenure_months=tenure_months,
        monthly_income_minor=income,
        existing_emi_minor=existing,
        proposed_emi_minor=proposed,
        dti_existing=dti_existing,
        dti_with_loan=dti_with_loan,
        max_eligible_amount_minor=at_tenure if eligible or not lockouts else 0,
        tenor_options=options if not lockouts else [],
        reasons=reasons,
    )
