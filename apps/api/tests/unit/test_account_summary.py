"""TASK-109: per-currency totals never add different currencies, and only deposit accounts count."""

import pytest
from pbip_api.modules.accounts.models import Account, AccountStatus, AccountType
from pbip_api.modules.accounts.service import summarise_balances

pytestmark = pytest.mark.unit


def _account(
    currency: str,
    ledger: int,
    held: int = 0,
    account_type: AccountType = AccountType.SAVINGS,
    status: AccountStatus = AccountStatus.ACTIVE,
) -> Account:
    return Account(
        account_number="400000000000",
        account_type=account_type,
        currency=currency,
        status=status,
        ledger_balance=ledger,
        held_amount=held,
        overdraft_limit=0,
    )


def test_each_currency_is_totalled_separately_and_ordered_by_code() -> None:
    totals = summarise_balances(
        [
            _account("USD", 5_00),
            _account("INR", 1_000_00, held=200_00),
            _account("INR", 300_00, account_type=AccountType.CURRENT),
            _account("USD", 2_50, status=AccountStatus.FROZEN),
        ]
    )
    assert [t.model_dump() for t in totals] == [
        {
            "currency": "INR",
            "ledger_balance_minor": 1_300_00,
            "held_minor": 200_00,
            "available_minor": 1_100_00,
            "account_count": 2,
            "frozen_account_count": 0,
        },
        {
            "currency": "USD",
            "ledger_balance_minor": 7_50,
            "held_minor": 0,
            "available_minor": 5_00,
            "account_count": 2,
            "frozen_account_count": 1,
        },
    ]


def test_system_and_closed_accounts_are_never_counted() -> None:
    assert (
        summarise_balances(
            [
                _account("INR", -9_999_00, account_type=AccountType.SYSTEM),
                _account("INR", 1_00, status=AccountStatus.CLOSED),
            ]
        )
        == []
    )


def test_an_overdrawn_account_lowers_the_total_and_the_overdraft_is_not_added() -> None:
    overdrawn = _account("INR", -50_00, account_type=AccountType.CURRENT)
    overdrawn.overdraft_limit = 1_000_00
    [total] = summarise_balances([overdrawn, _account("INR", 80_00)])
    assert (total.ledger_balance_minor, total.available_minor) == (30_00, 30_00)
