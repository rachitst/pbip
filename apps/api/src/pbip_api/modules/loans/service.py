"""Loan catalogue, EMI calculation, eligibility, applications and repayments (TASK-045/046/047).

Eligibility uses the customer's own ledger figures: average monthly income (analytics cash flow)
and the share of it already going to loan repayments (health score). The rules, in order:
1. A lockout from repeated failed sign-ins in the last 90 days refuses the check (account risk).
2. Debt-to-income (existing repayments + the new EMI) above 50% refuses the loan.
3. Otherwise the largest affordable amount per tenor keeps total repayments within 50% of income.
Every result explains itself with the figures it used. "Existing repayments" also include the
EMIs of the customer's active PBIP loans, so loans cannot be stacked before the first EMI posts.
Without an amount (FR-38, the assistant's "can I afford a loan?"), :func:`suggest_loan` offers
the rule-3 maximum, or says why it cannot.

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
from pbip_api.modules.accounts import service as accounts_service
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
    return income, existing + _active_loan_emis(session, customer_id)


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
    income, existing = _income_and_debt(session, customer_id, now)
    if income == 0:
        reason = "We could not find regular income in your account history to base an amount on."
        return LoanSuggestion("ASK_AMOUNT", product, tenure_months, 0, 0, [reason])
    options = _tenor_options(product, max(int(income * MAX_DTI) - existing, 0))
    pick = next((o for o in options if o.tenure_months == tenure_months), None)
    pick = pick or next((o for o in options if o.tenure_months > tenure_months), None)
    if pick is None:
        return LoanSuggestion(
            "NOT_AFFORDABLE", product, tenure_months, 0, 0, [_no_room(product, income, existing)]
        )
    check = check_eligibility(
        session,
        customer_id=customer_id,
        user_id=user_id,
        product_id=product.id,
        amount_minor=pick.max_amount_minor,
        tenure_months=pick.tenure_months,
        now=now,
    )
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


def _no_room(product: LoanProduct, income: int, existing: int) -> str:
    share = existing / income
    return (
        f"Loan repayments already take {share:.0%} of your income of "
        f"{format_minor(income, 'INR')} a month, which leaves no room for the smallest "
        f"{product.name.lower()} ({format_minor(product.min_amount_minor, 'INR')}) within the "
        "50% limit."
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
