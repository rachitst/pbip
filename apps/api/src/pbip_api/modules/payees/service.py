"""Payee use cases, including a simulated 'Confirmation of Payee' name check."""

from difflib import SequenceMatcher
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.core.errors import ConflictError, UnprocessableError
from pbip_api.modules.accounts.models import Account, AccountHolder, AccountType
from pbip_api.modules.accounts.service import is_valid_account_number
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger.models import Transaction
from pbip_api.modules.payees.models import Payee, PayeeStatus

_MAX_PAYEES = 50
_NAME_MATCH_THRESHOLD = 0.85
NameMatch = Literal["MATCH", "NO_MATCH", "NOT_CHECKED"]


def _normalise_name(name: str) -> str:
    return " ".join(name.lower().split())


class PayeeService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def create(
        self,
        customer_id: UUID,
        name: str,
        account_number: str,
        bank_code: str,
        nickname: str | None,
    ) -> Payee:
        if bank_code == self.settings.internal_bank_code and not is_valid_account_number(
            account_number
        ):
            raise UnprocessableError("invalid account number")
        count = self.session.execute(
            select(func.count()).select_from(Payee).where(Payee.customer_id == customer_id)
        ).scalar_one()
        if count >= _MAX_PAYEES:
            raise UnprocessableError("payee limit reached")
        payee = Payee(
            customer_id=customer_id,
            name=name.strip(),
            account_number=account_number,
            bank_code=bank_code,
            nickname=nickname,
        )
        self.session.add(payee)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("this payee already exists") from exc
        return payee

    def list(self, customer_id: UUID) -> list[Payee]:
        return list(
            self.session.execute(
                select(Payee)
                .where(Payee.customer_id == customer_id)
                .order_by(Payee.created_at.desc())
            )
            .scalars()
            .all()
        )

    def verify(self, payee: Payee) -> NameMatch:
        """Internal payees: compare the typed name with the real holder's name (never revealed).

        External payees cannot be checked in this simulation and are accepted as NOT_CHECKED.
        """
        if payee.status == PayeeStatus.BLOCKED:
            raise ConflictError("payee is blocked")
        result: NameMatch = "NOT_CHECKED"
        if payee.bank_code == self.settings.internal_bank_code:
            holder_name = self.session.execute(
                select(Customer.full_name)
                .join(AccountHolder, AccountHolder.customer_id == Customer.id)
                .join(Account, Account.id == AccountHolder.account_id)
                .where(
                    Account.account_number == payee.account_number,
                    Account.account_type != AccountType.SYSTEM,
                )
            ).scalar_one_or_none()
            if holder_name is None:
                raise UnprocessableError("payee account could not be found")
            ratio = SequenceMatcher(
                None, _normalise_name(payee.name), _normalise_name(holder_name)
            ).ratio()
            result = "MATCH" if ratio >= _NAME_MATCH_THRESHOLD else "NO_MATCH"
            if result == "NO_MATCH":
                return result
        payee.status = PayeeStatus.VERIFIED
        payee.verified_at = utcnow()
        self.session.commit()
        return result

    def delete(self, payee: Payee) -> None:
        used = self.session.execute(
            select(func.count()).select_from(Transaction).where(Transaction.payee_id == payee.id)
        ).scalar_one()
        if used:
            payee.status = PayeeStatus.BLOCKED  # keep history intact; just unusable
        else:
            self.session.delete(payee)
        self.session.commit()
