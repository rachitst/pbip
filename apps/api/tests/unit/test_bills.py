"""Pure-Python unit tests for bill payment models, schemas, policy, hashing, and notifications."""

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pbip_api.core.errors import NotFoundError
from pbip_api.modules.bills.models import (
    Biller,
    BillerCategory,
    BillerStatus,
    BillPayment,
    BillPaymentStatus,
)
from pbip_api.modules.bills.policy import get_owned_bill_payment
from pbip_api.modules.bills.schemas import BillerOut, BillPaymentOut, PayBillRequest
from pbip_api.modules.bills.service import BillsService, PayBillCommand
from pbip_api.modules.ledger.models import TransactionType
from pbip_api.modules.notifications.models import OutboxEvent
from pbip_api.modules.notifications.service import BILL_PAYMENT_POSTED, _render
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

pytestmark = pytest.mark.unit


def test_biller_and_bill_payment_models_define_expected_tables_and_constraints() -> None:
    assert Biller.__tablename__ == "billers"
    assert BillPayment.__tablename__ == "bill_payments"
    assert TransactionType.BILL_PAYMENT.value == "BILL_PAYMENT"

    biller_checks = {c.name for c in Biller.__table__.constraints if isinstance(c, CheckConstraint)}
    assert "ck_billers_category_valid" in biller_checks
    assert "ck_billers_status_valid" in biller_checks

    payment_checks = {
        c.name for c in BillPayment.__table__.constraints if isinstance(c, CheckConstraint)
    }
    assert "ck_bill_payments_status_valid" in payment_checks
    assert "ck_bill_payments_amount_positive" in payment_checks

    payment_fks = {fk.target_fullname for fk in BillPayment.__table__.foreign_keys}
    assert payment_fks == {
        "customers.id",
        "accounts.id",
        "billers.id",
        "transactions.id",
    }


def test_pay_bill_request_validates_bounds_and_currency() -> None:
    account_id = uuid4()
    biller_id = uuid4()
    valid = PayBillRequest(
        from_account_id=account_id,
        biller_id=biller_id,
        consumer_reference="CA-987654321",
        amount_minor=150_000,
    )
    assert valid.currency == "INR"
    assert valid.amount_minor == 150_000

    with pytest.raises(ValidationError):
        PayBillRequest(
            from_account_id=account_id,
            biller_id=biller_id,
            consumer_reference="CA-1",
            amount_minor=0,
        )

    with pytest.raises(ValidationError):
        PayBillRequest(
            from_account_id=account_id,
            biller_id=biller_id,
            consumer_reference="CA-1",
            amount_minor=-500,
        )

    with pytest.raises(ValidationError):
        PayBillRequest(
            from_account_id=account_id,
            biller_id=biller_id,
            consumer_reference="X",
            amount_minor=100,
        )

    with pytest.raises(ValidationError):
        PayBillRequest(
            from_account_id=account_id,
            biller_id=biller_id,
            consumer_reference="A" * 65,
            amount_minor=100,
        )

    with pytest.raises(ValidationError):
        PayBillRequest(
            from_account_id=account_id,
            biller_id=biller_id,
            consumer_reference="CA-12345",
            amount_minor=100,
            currency="inr",
        )


@pytest.mark.parametrize(
    "bad_ref",
    ["nul\x00byte", "line\nbreak", "tab\tchar", "del\x7fchar", "surrogate\ud800char"],
)
def test_pay_bill_request_rejects_control_characters_and_lone_surrogates(bad_ref: str) -> None:
    with pytest.raises(ValidationError):
        PayBillRequest(
            from_account_id=uuid4(),
            biller_id=uuid4(),
            consumer_reference=bad_ref,
            amount_minor=1000,
        )


def test_request_hash_is_deterministic_and_sensitive_to_every_field() -> None:
    user_id = uuid4()
    account_id = uuid4()
    biller_id = uuid4()
    base = PayBillCommand(
        from_account_id=account_id,
        biller_id=biller_id,
        consumer_reference="CA-100200",
        amount_minor=50_000,
        currency="INR",
    )
    h1 = BillsService._request_hash(user_id, base)
    h2 = BillsService._request_hash(user_id, base)
    assert h1 == h2 and len(h1) == 64

    assert h1 != BillsService._request_hash(uuid4(), base)
    assert h1 != BillsService._request_hash(
        user_id,
        PayBillCommand(
            from_account_id=uuid4(),
            biller_id=biller_id,
            consumer_reference="CA-100200",
            amount_minor=50_000,
            currency="INR",
        ),
    )
    assert h1 != BillsService._request_hash(
        user_id,
        PayBillCommand(
            from_account_id=account_id,
            biller_id=uuid4(),
            consumer_reference="CA-100200",
            amount_minor=50_000,
            currency="INR",
        ),
    )
    assert h1 != BillsService._request_hash(
        user_id,
        PayBillCommand(
            from_account_id=account_id,
            biller_id=biller_id,
            consumer_reference="CA-999999",
            amount_minor=50_000,
            currency="INR",
        ),
    )
    assert h1 != BillsService._request_hash(
        user_id,
        PayBillCommand(
            from_account_id=account_id,
            biller_id=biller_id,
            consumer_reference="CA-100200",
            amount_minor=50_001,
            currency="INR",
        ),
    )


def test_policy_get_owned_bill_payment_returns_row_or_raises_not_found() -> None:
    customer_id = uuid4()
    payment_id = uuid4()
    expected = BillPayment(
        id=payment_id,
        customer_id=customer_id,
        account_id=uuid4(),
        biller_id=uuid4(),
        transaction_id=uuid4(),
        consumer_reference="CA-12345",
        amount=10_000,
        currency="INR",
        status=BillPaymentStatus.POSTED,
    )

    session_found = MagicMock()
    session_found.execute.return_value.scalar_one_or_none.return_value = expected
    assert get_owned_bill_payment(session_found, customer_id, payment_id) is expected

    session_missing = MagicMock()
    session_missing.execute.return_value.scalar_one_or_none.return_value = None
    with pytest.raises(NotFoundError, match="bill payment not found"):
        get_owned_bill_payment(session_missing, customer_id, payment_id)


def test_build_result_shapes_receipt_with_reference_and_new_balance() -> None:
    now = datetime.now(UTC)
    biller = Biller(
        id=uuid4(),
        name="Maharashtra State Electricity Board",
        category=BillerCategory.ELECTRICITY,
        biller_code="MSEB-MH",
        status=BillerStatus.ACTIVE,
        created_at=now,
    )
    payment = BillPayment(
        id=uuid4(),
        customer_id=uuid4(),
        account_id=uuid4(),
        biller_id=biller.id,
        transaction_id=uuid4(),
        consumer_reference="CA-555666",
        amount=125_000,
        currency="INR",
        status=BillPaymentStatus.POSTED,
        created_at=now,
    )
    result = BillsService._build_result(
        payment,
        biller=biller,
        reference="BILL/MSEB-MH/CA-555666",
        new_balance_minor=875_000,
        created=True,
    )
    assert result.status_code == 201
    validated = BillPaymentOut.model_validate(result.body)
    assert validated.amount_minor == 125_000
    assert validated.reference == "BILL/MSEB-MH/CA-555666"
    assert validated.new_balance_minor == 875_000
    assert validated.biller_code == "MSEB-MH"

    biller_out = BillerOut.model_validate(biller, from_attributes=True)
    assert biller_out.category == BillerCategory.ELECTRICITY


def test_notification_renders_bill_payment_posted_event() -> None:
    with_biller = OutboxEvent(
        event_type=BILL_PAYMENT_POSTED,
        payload={
            "user_id": str(uuid4()),
            "amount_minor": 125_000,
            "currency": "INR",
            "biller_name": "Maharashtra State Electricity Board",
        },
    )
    rendered = _render(with_biller)
    assert rendered is not None
    title, body = rendered
    assert title == "Bill payment completed"
    assert "Maharashtra State Electricity Board" in body
    assert "1,250.00" in body

    without_biller = OutboxEvent(
        event_type=BILL_PAYMENT_POSTED,
        payload={
            "user_id": str(uuid4()),
            "amount_minor": 50_000,
            "currency": "INR",
        },
    )
    rendered_plain = _render(without_biller)
    assert rendered_plain == (
        "Bill payment completed",
        "Your bill payment of INR 500.00 was completed.",
    )
