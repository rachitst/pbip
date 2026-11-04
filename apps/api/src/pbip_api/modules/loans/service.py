"""Loan catalogue, EMI calculation, eligibility, applications and repayments (TASK-045/046/047).

Eligibility uses the customer's own figures (``loan-eligibility-v2``, ADR-0056): average monthly
income and spending (analytics cash flow), the share of income already going to loan repayments
(health score) and the available balance across the customer's accounts. The rules, in order:
1. A lockout from repeated failed sign-ins in the last 90 days refuses the check (account risk).
2. No regular income refuses it.
3. Debt-to-income (existing repayments + the new EMI) above 50% refuses the loan.
4. Spending + active PBIP EMIs + the new EMI above 90% of income refuses it (UC-06 spending).
5. An available balance below one new EMI refuses it (UC-06 balances: the first EMI is covered).
The largest affordable amount per tenor uses the tightest of the budgets of rules 3-5, so a
suggested maximum always passes them. Every result explains itself with the figures it used.
"Existing repayments" also include the EMIs of the customer's active PBIP loans, so loans cannot
be stacked before the first EMI posts.
Without an amount (FR-38, the assistant's "can I afford a loan?"), :func:`suggest_loan` offers
the rules 3-5 maximum, or says why it cannot.

Applying (TASK-047, ADR-0024) re-runs eligibility under the per-user lock and, in ONE database
transaction, disburses through the ledger, stores the loan and its full schedule and creates the
loan's monthly repayment instruction on the scheduled transfers engine.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.core.errors import ConflictError, NotFoundError, UnprocessableError
from pbip_api.core.metrics import IDEMPOTENT_REPLAYS_TOTAL
from pbip_api.core.money import format_minor
from pbip_api.modules.accounts.models import AccountStatus
from pbip_api.modules.accounts.policy import get_owned_account
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.auth.service import RequestMeta, recent_lockouts
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import IdempotencyKey, Transaction
from pbip_api.modules.loans import emi as emi_math
from pbip_api.modules.loans.catalogue import PRODUCTS, LoanProduct, LoanType, get_product
from pbip_api.modules.loans.due_dates import due_dates
from pbip_api.modules.loans.models import InstalmentStatus, Loan, LoanInstalment, LoanStatus
from pbip_api.modules.loans.policy import get_owned_loan, list_owned_loans
from pbip_api.modules.loans.schemas import (
    EligibilityOut,
    EmiOut,
    InstalmentOut,
    LoanApplyRequest,
    LoanInstalmentOut,
    LoanOfferOut,
    LoanOffersOut,
    LoanOut,
    LoanProductOut,
    LoanSummaryOut,
    TenorOption,
)
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.payments.service import (
    IdempotencyKeyReuseError,
    create_loan_repayment_instruction,
)

MAX_DTI = 0.50
MAX_SPENDING_PERCENT = 90  # rule 4: spending, PBIP EMIs and the new EMI, as a share of income
MIN_BALANCE_EMIS = 1  # rule 5: the available balance covers at least this many new EMIs
RULES_VERSION = "loan-eligibility-v2"
LOCKOUT_LOOKBACK = timedelta(days=90)
# The tenor a loan offer is sized at (``loan_offers``): the middle of each product's range, a
# common term for that kind of loan. A longer offered tenor is used only if none fits here.
OFFER_TENURE_MONTHS: dict[LoanType, int] = {
    LoanType.PERSONAL: 36,
    LoanType.AUTO: 60,
    LoanType.HOME: 240,
}


@dataclass(frozen=True, slots=True)
class EligibilityFigures:
    """The customer's own figures every eligibility rule reads (ADR-0056). Minor units."""

    income_minor: int
    existing_emi_minor: int  # rule 3: ledger repayments + active PBIP EMIs
    active_loan_emi_minor: int  # PBIP EMIs alone, added to spending in rule 4 (safe side)
    monthly_spend_minor: int
    available_balance_minor: int

    @property
    def dti_room_minor(self) -> int:
        return int(self.income_minor * MAX_DTI) - self.existing_emi_minor

    @property
    def spending_room_minor(self) -> int:
        cap = self.income_minor * MAX_SPENDING_PERCENT // 100
        return cap - self.monthly_spend_minor - self.active_loan_emi_minor

    @property
    def balance_room_minor(self) -> int:
        return self.available_balance_minor // MIN_BALANCE_EMIS

    @property
    def budget_minor(self) -> int:
        """The largest new EMI rules 3-5 all allow."""
        return max(min(self.dti_room_minor, self.spending_room_minor, self.balance_room_minor), 0)


def figures_from(
    *,
    income_minor: int,
    emi_to_income: float,
    monthly_spend_minor: int,
    active_loan_emi_minor: int,
    available_balance_minor: int,
) -> EligibilityFigures:
    """The one formula for the figures, used by the checks here and by the recommendation engine's
    cached inputs (ADR-0054, ADR-0056), so both always assess a loan alike."""
    existing = round(emi_to_income * income_minor) + active_loan_emi_minor
    return EligibilityFigures(
        income_minor=income_minor,
        existing_emi_minor=existing,
        active_loan_emi_minor=active_loan_emi_minor,
        monthly_spend_minor=monthly_spend_minor,
        available_balance_minor=available_balance_minor,
    )


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


def _figures(session: Session, customer_id: UUID, now: datetime) -> EligibilityFigures:
    """Income, spending and the repayment ratio of the first account (one entries pass, #343),
    and the available balance of all the customer's accounts."""
    profile = analytics_service.get_customer_financial_profile(session, customer_id, now)
    if profile is None:
        return EligibilityFigures(0, 0, 0, 0, 0)
    return figures_from(
        income_minor=profile.cash_flow.monthly_income_minor,
        emi_to_income=profile.health.components.emi_to_income.metric_value,
        monthly_spend_minor=profile.cash_flow.monthly_spend_minor,
        active_loan_emi_minor=_active_loan_emis(session, customer_id),
        available_balance_minor=sum(account.available_balance for account in profile.accounts),
    )


def _active_loan_emis(session: Session, customer_id: UUID) -> int:
    """EMIs PBIP itself is owed. Counted on top of the ledger figure, which only sees repayments
    already made; after repayments start this over-counts slightly, the safe side (ADR-0024)."""
    total = session.execute(
        select(func.coalesce(func.sum(Loan.emi), 0)).where(
            Loan.customer_id == customer_id, Loan.status == LoanStatus.ACTIVE
        )
    ).scalar_one()
    return int(total)


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
    figures = _figures(session, customer_id, now)
    lockouts = recent_lockouts(session, user_id, now - LOCKOUT_LOOKBACK)
    return assess(product, amount_minor, tenure_months, figures, lockouts)


def _inr(minor: int) -> str:
    return format_minor(minor, "INR")


def _debt_failure(figures: EligibilityFigures, proposed: int, dti_existing: float) -> str:
    income = figures.income_minor
    if dti_existing > MAX_DTI:
        return (
            f"Loan repayments already take {dti_existing:.0%} of your income of {_inr(income)} "
            "a month, above the 50% limit."
        )
    dti_with_loan = (figures.existing_emi_minor + proposed) / income
    return (
        f"With an EMI of {_inr(proposed)}, repayments would take {dti_with_loan:.0%} of your "
        f"income of {_inr(income)}, above the 50% limit."
    )


def _spending_reason(figures: EligibilityFigures, proposed: int, ok: bool) -> str:
    committed = figures.monthly_spend_minor + figures.active_loan_emi_minor + proposed
    share = committed / figures.income_minor
    limit = f"{MAX_SPENDING_PERCENT}% limit"
    if ok:
        return (
            f"Your spending of {_inr(figures.monthly_spend_minor)} a month plus this EMI comes to "
            f"{share:.1%} of your income, within the {limit}."
        )
    return (
        f"Your spending of {_inr(figures.monthly_spend_minor)} a month plus an EMI of "
        f"{_inr(proposed)} would take {share:.1%} of your income of "
        f"{_inr(figures.income_minor)}, above the {limit}."
    )


def _balance_reason(figures: EligibilityFigures, proposed: int, ok: bool) -> str:
    balance = _inr(figures.available_balance_minor)
    if ok:
        return f"Your balance of {balance} already covers the first EMI of {_inr(proposed)}."
    return (
        f"Your balance of {balance} does not cover one EMI of {_inr(proposed)}; we look for at "
        "least the first EMI in your accounts before lending."
    )


def assess(
    product: LoanProduct,
    amount_minor: int,
    tenure_months: int,
    figures: EligibilityFigures,
    lockouts: int,
) -> EligibilityOut:
    """Rules 1-5 on the customer's figures (pure: no I/O). ``check_eligibility``, ``suggest_loan``
    and ``loan_offers`` all decide through here, so they cannot disagree (ADR-0056)."""
    income, existing = figures.income_minor, figures.existing_emi_minor
    proposed = emi_math.emi(amount_minor, product.annual_rate_bps, tenure_months)
    dti_existing = round(existing / income, 4) if income else 0.0
    dti_with_loan = round((existing + proposed) / income, 4) if income else 1.0
    committed = figures.monthly_spend_minor + figures.active_loan_emi_minor + proposed
    spending_ratio = round(committed / income, 4) if income else 1.0
    spending_ok = committed <= income * MAX_SPENDING_PERCENT // 100
    balance_ok = figures.available_balance_minor >= proposed * MIN_BALANCE_EMIS
    options = _tenor_options(product, figures.budget_minor)
    at_tenure = next((o.max_amount_minor for o in options if o.tenure_months == tenure_months), 0)

    reasons: list[str] = []
    if lockouts:
        reasons.append(
            f"Your account was locked {lockouts} time(s) in the last 90 days after repeated "
            "failed sign-ins; loan checks resume 90 days after the last lockout."
        )
    if income == 0:
        reasons.append("We could not find regular income in your account history.")
    else:
        if dti_with_loan > MAX_DTI:
            reasons.append(_debt_failure(figures, proposed, dti_existing))
        if not spending_ok:
            reasons.append(_spending_reason(figures, proposed, ok=False))
        if not balance_ok:
            reasons.append(_balance_reason(figures, proposed, ok=False))
    eligible = not reasons
    if eligible:
        reasons += [
            f"An EMI of {_inr(proposed)} brings repayments to {dti_with_loan:.0%} of your income "
            f"of {_inr(income)}, within the 50% limit.",
            _spending_reason(figures, proposed, ok=True),
            _balance_reason(figures, proposed, ok=True),
        ]
    if at_tenure and amount_minor > at_tenure:
        reasons.append(f"Over {tenure_months} months you could borrow up to {_inr(at_tenure)}.")
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
        monthly_spend_minor=figures.monthly_spend_minor,
        available_balance_minor=figures.available_balance_minor,
        spending_with_loan_ratio=spending_ratio,
        rules_version=RULES_VERSION,
    )


@dataclass(frozen=True, slots=True)
class LoanSuggestion:
    """FR-38: the answer to "can I afford a loan?" when the customer names no amount.

    ``SUGGESTED``: the largest amount eligibility allows at ``tenure_months`` (``reasons`` are the
    eligibility check's own, run again for that amount). ``ASK_AMOUNT``: eligibility cannot be
    computed (no income history, or a recent lockout); ``reasons`` say which. ``NOT_AFFORDABLE``:
    it was computed and no amount of this product fits within the 50% limit.
    """

    status: Literal["SUGGESTED", "ASK_AMOUNT", "NOT_AFFORDABLE"]
    product: LoanProduct
    tenure_months: int
    amount_minor: int  # 0 unless SUGGESTED
    emi_minor: int  # 0 unless SUGGESTED
    reasons: list[str]


def suggest_loan(
    session: Session,
    *,
    customer_id: UUID,
    user_id: UUID,
    product_id: UUID,
    tenure_months: int,
    now: datetime,
) -> LoanSuggestion:
    """Read-only. Same rules and figures as :func:`check_eligibility`; no amount is invented: the
    suggestion is the TASK-046 maximum for this tenure (or, if none fits there, the shortest
    offered tenure that has one), from the catalogue rate."""
    product = get_product(product_id)
    if product is None:
        raise NotFoundError("loan product not found")
    _product(product.id, product.min_amount_minor, tenure_months)  # tenure offered, or 422
    if recent_lockouts(session, user_id, now - LOCKOUT_LOOKBACK):
        reason = (
            "Your account was locked after repeated failed sign-ins in the last 90 days, so I "
            "can't work out a loan amount for you until 90 days after the last lockout."
        )
        return LoanSuggestion("ASK_AMOUNT", product, tenure_months, 0, 0, [reason])
    figures = _figures(session, customer_id, now)
    if figures.income_minor == 0:
        reason = "We could not find regular income in your account history to base an amount on."
        return LoanSuggestion("ASK_AMOUNT", product, tenure_months, 0, 0, [reason])
    pick = _pick_tenor(product, figures, tenure_months)
    if pick is None:
        return LoanSuggestion(
            "NOT_AFFORDABLE", product, tenure_months, 0, 0, [_no_room(product, figures)]
        )
    check = assess(product, pick.max_amount_minor, pick.tenure_months, figures, lockouts=0)
    if check.status != "ELIGIBLE":  # cannot happen with the same figures; never suggest anyway
        return LoanSuggestion("NOT_AFFORDABLE", product, tenure_months, 0, 0, check.reasons)
    return LoanSuggestion(
        "SUGGESTED",
        product,
        pick.tenure_months,
        pick.max_amount_minor,
        pick.emi_at_max_minor,
        check.reasons,
    )


def _pick_tenor(
    product: LoanProduct, figures: EligibilityFigures, tenure_months: int
) -> TenorOption | None:
    """The largest affordable amount at ``tenure_months``, else at the next longer tenor that has
    one; ``None`` when no tenor fits the smallest amount."""
    options = _tenor_options(product, figures.budget_minor)
    pick = next((o for o in options if o.tenure_months == tenure_months), None)
    return pick or next((o for o in options if o.tenure_months > tenure_months), None)


def _no_room(product: LoanProduct, figures: EligibilityFigures) -> str:
    """Names the rule that binds: the smallest of the three budgets (ties: the debt rule)."""
    income = figures.income_minor
    smallest = f"the smallest {product.name.lower()} ({_inr(product.min_amount_minor)})"
    rooms = (figures.dti_room_minor, figures.spending_room_minor, figures.balance_room_minor)
    if figures.dti_room_minor == min(rooms):
        share = figures.existing_emi_minor / income
        return (
            f"Loan repayments already take {share:.0%} of your income of {_inr(income)} a month, "
            f"which leaves no room for {smallest} within the 50% limit."
        )
    if figures.spending_room_minor == min(rooms):
        share = (figures.monthly_spend_minor + figures.active_loan_emi_minor) / income
        return (
            f"Your spending of {_inr(figures.monthly_spend_minor)} a month already takes "
            f"{share:.1%} of your income of {_inr(income)}, which leaves no room for {smallest} "
            f"within the {MAX_SPENDING_PERCENT}% limit."
        )
    return (
        f"Your balance of {_inr(figures.available_balance_minor)} does not cover the first EMI "
        f"of {smallest}; we look for at least the first EMI in your accounts before lending."
    )


def loan_offers(
    session: Session, *, customer_id: UUID, user_id: UUID, now: datetime | None = None
) -> LoanOffersOut:
    """UC-06: every catalogue loan, assessed on the customer's own figures (read-only)."""
    now = now or utcnow()
    figures = _figures(session, customer_id, now)
    lockouts = recent_lockouts(session, user_id, now - LOCKOUT_LOOKBACK)
    return offers_from(figures, lockouts)


def recent_lockout_count(session: Session, user_id: UUID, now: datetime) -> int:
    """Rule 1's input, for callers that bring their own cached figures (recommendations)."""
    return recent_lockouts(session, user_id, now - LOCKOUT_LOOKBACK)


def offers_from(figures: EligibilityFigures, lockouts: int) -> LoanOffersOut:
    """Pure: one offer per catalogue product, sized at its offer tenor (ADR-0056)."""
    return LoanOffersOut(
        offers=[_offer(product, figures, lockouts) for product in PRODUCTS],
        monthly_income_minor=figures.income_minor,
        monthly_spend_minor=figures.monthly_spend_minor,
        available_balance_minor=figures.available_balance_minor,
        rules_version=RULES_VERSION,
    )


def _offer(product: LoanProduct, figures: EligibilityFigures, lockouts: int) -> LoanOfferOut:
    tenure = OFFER_TENURE_MONTHS[product.loan_type]
    pick = None if lockouts or figures.income_minor == 0 else _pick_tenor(product, figures, tenure)
    if pick is None:
        check = assess(product, product.min_amount_minor, tenure, figures, lockouts)
        reasons = check.reasons if check.status == "NOT_ELIGIBLE" else [_no_room(product, figures)]
        return _offer_out(product, tenure, reasons)
    check = assess(product, pick.max_amount_minor, pick.tenure_months, figures, lockouts)
    if check.status != "ELIGIBLE":  # cannot happen with the same figures; never offer anyway
        return _offer_out(product, pick.tenure_months, check.reasons)
    return LoanOfferOut(
        product_id=product.id,
        loan_type=product.loan_type.value,
        name=product.name,
        annual_rate_bps=product.annual_rate_bps,
        status="ELIGIBLE",
        suggested_min_amount_minor=product.min_amount_minor,
        suggested_max_amount_minor=pick.max_amount_minor,
        tenure_months=pick.tenure_months,
        emi_at_max_minor=pick.emi_at_max_minor,
        reasons=check.reasons,
    )


def _offer_out(product: LoanProduct, tenure: int, reasons: list[str]) -> LoanOfferOut:
    return LoanOfferOut(
        product_id=product.id,
        loan_type=product.loan_type.value,
        name=product.name,
        annual_rate_bps=product.annual_rate_bps,
        status="NOT_ELIGIBLE",
        suggested_min_amount_minor=0,
        suggested_max_amount_minor=0,
        tenure_months=tenure,
        emi_at_max_minor=0,
        reasons=reasons,
    )


# ---------------------------------------------------------------- applications (TASK-047)
class LoanNotEligibleError(UnprocessableError):
    code = "loan_not_eligible"


@dataclass(frozen=True, slots=True)
class ApplyResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool = False


def apply_for_loan(
    session: Session,
    settings: Settings,
    *,
    customer_id: UUID,
    user_id: UUID,
    request: LoanApplyRequest,
    idempotency_key: str,
    meta: RequestMeta,
) -> ApplyResult:
    """One use case, one commit (ADR-0024). Lock order (AGENTS.md section 4): user advisory lock,
    idempotency row, then the accounts FOR UPDATE inside ``ledger.disburse_loan`` BEFORE the loan,
    instalment and instruction rows (their foreign keys take KEY SHARE locks on the accounts)."""
    product = _product(request.product_id, request.amount_minor, request.tenure_months)
    _lock_user(session, user_id)
    idem_id, replay = _begin_idempotent(
        session, settings, user_id, idempotency_key, _apply_hash(user_id, request)
    )
    if replay is not None:
        return replay

    account = get_owned_account(session, customer_id, request.account_id)
    if account.status != AccountStatus.ACTIVE:
        raise UnprocessableError("the loan account must be active")
    if account.currency != "INR":
        raise UnprocessableError("loans are paid into INR accounts only")
    now = utcnow()
    eligibility = check_eligibility(
        session,
        customer_id=customer_id,
        user_id=user_id,
        product_id=product.id,
        amount_minor=request.amount_minor,
        tenure_months=request.tenure_months,
        now=now,
    )
    if eligibility.status != "ELIGIBLE":
        raise LoanNotEligibleError("This loan cannot be approved. " + " ".join(eligibility.reasons))

    loan = _disburse_and_book(session, product, request, customer_id, user_id, account.id, now)
    body = _loan_out(session, loan, now.date()).model_dump(mode="json")
    outcome = ApplyResult(201, body)
    _store_response(session, idem_id, outcome, loan.disbursal_transaction_id)
    AuditService(session).record(
        user_id,
        "LOAN_DISBURSED",
        "loan",
        str(loan.id),
        ip=meta.ip,
        request_id=meta.request_id,
        details={
            "product_id": str(product.id),
            "amount_minor": loan.principal,
            "tenure_months": loan.tenure_months,
            "annual_rate_bps": loan.annual_rate_bps,
            "transaction_id": str(loan.disbursal_transaction_id),
            "dti_with_loan": eligibility.dti_with_loan,
        },
    )
    session.commit()
    return outcome


def _disburse_and_book(
    session: Session,
    product: LoanProduct,
    request: LoanApplyRequest,
    customer_id: UUID,
    user_id: UUID,
    account_id: UUID,
    now: datetime,
) -> Loan:
    plan = emi_math.schedule(request.amount_minor, product.annual_rate_bps, request.tenure_months)
    dates = due_dates(now.date(), request.tenure_months)
    disbursal = ledger.disburse_loan(
        session,
        account_id=account_id,
        amount=request.amount_minor,
        currency="INR",
        initiated_by_user_id=user_id,
        reference=f"{product.name} disbursal",
    )
    loan = Loan(
        customer_id=customer_id,
        user_id=user_id,
        account_id=account_id,
        disbursal_transaction_id=disbursal.id,
        product_id=product.id,
        loan_type=product.loan_type,
        annual_rate_bps=product.annual_rate_bps,
        tenure_months=request.tenure_months,
        principal=request.amount_minor,
        emi=plan.emi_minor,
        total_interest=plan.total_interest_minor,
        currency="INR",
        status=LoanStatus.ACTIVE,
    )
    session.add(loan)
    session.flush()
    session.add_all(
        LoanInstalment(
            loan_id=loan.id,
            seq=row.month,
            due_date=due,
            amount=row.payment_minor,
            principal=row.principal_minor,
            interest=row.interest_minor,
            balance_after=row.balance_after_minor,
            status=InstalmentStatus.DUE,
        )
        for row, due in zip(plan.instalments, dates, strict=True)
    )
    create_loan_repayment_instruction(
        session,
        customer_id=customer_id,
        user_id=user_id,
        account_id=account_id,
        loan_id=loan.id,
        emi_minor=plan.emi_minor,
        currency="INR",
        first_due=dates[0],
        last_due=dates[-1],
        reference=f"{product.name} EMI",
    )
    notifications.enqueue(
        session,
        notifications.LOAN_DISBURSED,
        {
            "user_id": str(user_id),
            "transaction_id": str(disbursal.id),
            "account_id": str(account_id),
            "amount_minor": request.amount_minor,
            "currency": "INR",
            "emi_minor": plan.emi_minor,
            "first_due_date": dates[0].isoformat(),
        },
    )
    session.flush()
    return loan


def _lock_user(session: Session, user_id: UUID) -> None:
    """The same per-user advisory lock as transfers: ALWAYS the first lock of the request."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:u))"), {"u": str(user_id)})


def _apply_hash(user_id: UUID, request: LoanApplyRequest) -> str:
    """Includes an "op" field, so a key reused across endpoints is a reuse error, never a replay
    of another endpoint's response."""
    canonical = json.dumps(
        {"op": "loan.apply", "user": str(user_id)} | request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _begin_idempotent(
    session: Session, settings: Settings, user_id: UUID, key: str, request_hash: str
) -> tuple[UUID, ApplyResult | None]:
    """Claim the key. A concurrent duplicate BLOCKS on the unique index until the first commits
    (then replays its response) or rolls back (then runs itself)."""
    ledger.discard_expired_idempotency_key(session, user_id, key)
    inserted = session.execute(
        pg_insert(IdempotencyKey)
        .values(
            id=uuid4(),
            user_id=user_id,
            key=key,
            request_hash=request_hash,
            expires_at=utcnow() + timedelta(hours=settings.idempotency_ttl_hours),
        )
        .on_conflict_do_nothing(constraint="uq_idempotency_keys_user_key")
        .returning(IdempotencyKey.id)
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted, None
    existing = session.execute(
        select(IdempotencyKey).where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
    ).scalar_one()
    if existing.request_hash != request_hash:
        raise IdempotencyKeyReuseError("Idempotency-Key was already used with a different request")
    if existing.response_status is None or existing.response_body is None:
        raise ConflictError("a request with this Idempotency-Key is still in progress")
    IDEMPOTENT_REPLAYS_TOTAL.inc()
    return existing.id, ApplyResult(existing.response_status, existing.response_body, True)


def _store_response(
    session: Session, idem_id: UUID, outcome: ApplyResult, transaction_id: UUID
) -> None:
    row = session.get(IdempotencyKey, idem_id)
    if row is None:
        raise RuntimeError("idempotency row vanished")
    row.response_status = outcome.status_code
    row.response_body = outcome.body
    row.transaction_id = transaction_id


# ---------------------------------------------------------------------- reads
def _instalments(session: Session, loan_ids: Sequence[UUID]) -> dict[UUID, list[LoanInstalment]]:
    by_loan: dict[UUID, list[LoanInstalment]] = {loan_id: [] for loan_id in loan_ids}
    if not by_loan:
        return by_loan  # no loans, no instalments: skip the round trip (#343)
    rows = session.execute(
        select(LoanInstalment)
        .where(LoanInstalment.loan_id.in_(loan_ids))
        .order_by(LoanInstalment.loan_id, LoanInstalment.seq)
    ).scalars()
    for row in rows:
        by_loan[row.loan_id].append(row)
    return by_loan


def _status(instalment: LoanInstalment, today: date) -> str:
    if instalment.status == InstalmentStatus.PAID:
        return "PAID"
    return "OVERDUE" if instalment.due_date < today else "DUE"


def _summary_fields(loan: Loan, rows: list[LoanInstalment], today: date) -> dict[str, Any]:
    unpaid = [r for r in rows if r.status == InstalmentStatus.DUE]
    product = get_product(loan.product_id)
    return {
        "id": loan.id,
        "product_id": loan.product_id,
        "product_name": product.name if product else LoanType(loan.loan_type).value.title(),
        "loan_type": loan.loan_type.value,
        "status": loan.status.value,
        "account_id": loan.account_id,
        "currency": loan.currency,
        "principal_minor": loan.principal,
        "annual_rate_bps": loan.annual_rate_bps,
        "tenure_months": loan.tenure_months,
        "emi_minor": loan.emi,
        "total_interest_minor": loan.total_interest,
        "total_payable_minor": loan.principal + loan.total_interest,
        "outstanding_principal_minor": sum(r.principal for r in unpaid),
        "paid_instalments": len(rows) - len(unpaid),
        "overdue_instalments": sum(1 for r in unpaid if r.due_date < today),
        "next_due_date": unpaid[0].due_date if unpaid else None,
        "next_due_amount_minor": unpaid[0].amount if unpaid else None,
        "disbursed_at": loan.created_at,
    }


def _loan_out(session: Session, loan: Loan, today: date) -> LoanOut:
    rows = _instalments(session, [loan.id])[loan.id]
    return LoanOut(
        **_summary_fields(loan, rows, today),
        disbursal_transaction_id=loan.disbursal_transaction_id,
        instalments=[
            LoanInstalmentOut(
                seq=r.seq,
                due_date=r.due_date,
                amount_minor=r.amount,
                principal_minor=r.principal,
                interest_minor=r.interest,
                balance_after_minor=r.balance_after,
                status=_status(r, today),
                paid_at=r.paid_at,
            )
            for r in rows
        ],
    )


def list_loans(session: Session, customer_id: UUID) -> list[LoanSummaryOut]:
    """The authenticated customer's ACTIVE loans (closed loans stay readable by id)."""
    loans = list_owned_loans(session, customer_id, active_only=True)
    rows = _instalments(session, [loan.id for loan in loans])
    today = utcnow().date()
    return [LoanSummaryOut(**_summary_fields(loan, rows[loan.id], today)) for loan in loans]


def get_loan(session: Session, customer_id: UUID, loan_id: UUID) -> LoanOut:
    return _loan_out(session, get_owned_loan(session, customer_id, loan_id), utcnow().date())


# ------------------------------------------------------------- repayments (ADR-0024)
@dataclass(frozen=True, slots=True)
class RepaymentOutcome:
    """What one scheduler run did. ``transaction`` is None when nothing was due."""

    transaction: Transaction | None
    instalments_paid: int
    next_due_date: date | None
    closed: bool


def collect_due_instalments(session: Session, *, loan_id: UUID, on_date: date) -> RepaymentOutcome:
    """Collect EVERY unpaid instalment due on or before ``on_date`` (arrears first) in one ledger
    posting, inside the caller's transaction; the scheduler commits or rolls back.

    Called by the scheduled transfers engine after its instruction try-lock, user lock and
    instruction ``FOR UPDATE``; this then locks the loan and its due instalments, and the ledger
    locks the accounts before the header. An instalment is paid only while DUE under its row lock,
    so a retried or concurrent run finds nothing due and posts nothing.
    """
    loan = session.execute(
        select(Loan)
        .where(Loan.id == loan_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if loan is None:
        raise NotFoundError("loan not found")
    due = list(
        session.execute(
            select(LoanInstalment)
            .where(
                LoanInstalment.loan_id == loan.id,
                LoanInstalment.status == InstalmentStatus.DUE,
                LoanInstalment.due_date <= on_date,
            )
            .order_by(LoanInstalment.seq)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalars()
    )
    transaction = _post_repayment(session, loan, due) if due else None
    upcoming = session.execute(
        select(LoanInstalment.due_date)
        .where(LoanInstalment.loan_id == loan.id, LoanInstalment.status == InstalmentStatus.DUE)
        .order_by(LoanInstalment.seq)
        .limit(1)
    ).scalar_one_or_none()
    if upcoming is None and loan.status == LoanStatus.ACTIVE:
        loan.status = LoanStatus.CLOSED
        loan.closed_at = utcnow()
    session.flush()
    return RepaymentOutcome(transaction, len(due), upcoming, upcoming is None)


def _post_repayment(session: Session, loan: Loan, due: list[LoanInstalment]) -> Transaction:
    product = get_product(loan.product_id)
    name = product.name if product else "Loan"
    seqs = f"{due[0].seq}" if len(due) == 1 else f"{due[0].seq}-{due[-1].seq}"
    total = sum(i.amount for i in due)
    transaction = ledger.collect_loan_repayment(
        session,
        account_id=loan.account_id,
        amount=total,
        currency=loan.currency,
        initiated_by_user_id=loan.user_id,
        reference=f"{name} EMI {seqs}/{loan.tenure_months}",
    )
    paid_at = utcnow()
    for instalment in due:
        instalment.status = InstalmentStatus.PAID
        instalment.transaction_id = transaction.id
        instalment.paid_at = paid_at
    AuditService(session).record(
        None,
        "LOAN_REPAYMENT_POSTED",
        "loan",
        str(loan.id),
        details={
            "actor": "scheduler",
            "channel": "SCHEDULED",
            "transaction_id": str(transaction.id),
            "amount_minor": total,
            "instalments": [i.seq for i in due],
        },
    )
    notifications.enqueue(
        session,
        notifications.LOAN_REPAYMENT_POSTED,
        {
            "user_id": str(loan.user_id),
            "transaction_id": str(transaction.id),
            "account_id": str(loan.account_id),
            "amount_minor": total,
            "currency": loan.currency,
            "instalments_paid": len(due),
        },
    )
    return transaction
