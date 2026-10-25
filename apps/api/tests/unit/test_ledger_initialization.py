import pytest
from pbip_api.modules.accounts.models import Account, AccountType
from pbip_api.modules.ledger.service import initialize_account

pytestmark = pytest.mark.unit


def test_initialize_account_sets_zero_materialised_balances() -> None:
    account = Account(account_number="400000000000", account_type=AccountType.SAVINGS)
    initialize_account(account)
    assert (account.ledger_balance, account.held_amount) == (0, 0)
