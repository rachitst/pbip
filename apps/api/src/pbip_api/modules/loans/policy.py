"""Object-level authorization for loans (TASK-047).

The ONLY way application code obtains a loan on behalf of a customer. Someone else's loan and a
loan that does not exist are indistinguishable (404), so loan ids cannot be probed.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.errors import NotFoundError
from pbip_api.modules.loans.models import Loan, LoanStatus


def get_owned_loan(session: Session, customer_id: UUID, loan_id: UUID) -> Loan:
    loan = session.execute(
        select(Loan).where(Loan.id == loan_id, Loan.customer_id == customer_id)
    ).scalar_one_or_none()
    if loan is None:
        raise NotFoundError("loan not found")
    return loan


def list_owned_loans(session: Session, customer_id: UUID, *, active_only: bool) -> list[Loan]:
    stmt = select(Loan).where(Loan.customer_id == customer_id)
    if active_only:
        stmt = stmt.where(Loan.status == LoanStatus.ACTIVE)
    return list(session.execute(stmt.order_by(Loan.created_at.desc(), Loan.id.desc())).scalars())
