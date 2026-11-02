"""FR-08 / TASK-109: ``GET /api/v1/accounts/summary``, the total balance across the customer's own
accounts. Exact totals: own accounts only, joint accounts in full, FROZEN in the ledger total but
not in available, CLOSED left out, held amounts taken out of available."""

from collections.abc import Callable
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.accounts.models import Account, AccountHolder, AccountStatus, HolderRole
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import TransactionType
from pbip_test_support import Actor
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

URL = "/api/v1/accounts/summary"


def _open(client: TestClient, actor: Actor, account_type: str = "SAVINGS") -> UUID:
    response = client.post(
        "/api/v1/accounts", headers=actor.headers, json={"account_type": account_type}
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _set_status(db: Session, account_id: UUID, status: AccountStatus) -> None:
    db.get_one(Account, account_id).status = status
    db.commit()


def _hold(db: Session, source: UUID, target: UUID, amount: int) -> None:
    """A transfer under fraud review: the amount is reserved on the source (ledger-owned)."""
    txn = ledger.create_transaction(
        db,
        type_=TransactionType.TRANSFER,
        amount=amount,
        currency="INR",
        debit_account_id=source,
        credit_account_id=target,
    )
    ledger.place_hold(db, txn)
    db.commit()


def _totals(client: TestClient, actor: Actor) -> list[dict[str, object]]:
    response = client.get(URL, headers=actor.headers)
    assert response.status_code == 200, response.text
    totals: list[dict[str, object]] = response.json()["totals"]
    return totals


def test_summary_totals_own_accounts_with_hold_frozen_and_closed(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=100_000)
    bob = make_customer(funds=999_999)
    assert alice.account_id is not None and bob.account_id is not None
    current = _open(client, alice, "CURRENT")
    frozen = _open(client, alice)
    closed = _open(client, alice)
    ledger.deposit(db, current, 50_000)
    ledger.deposit(db, frozen, 7_000)
    ledger.deposit(db, closed, 3_000)
    db.commit()
    _set_status(db, frozen, AccountStatus.FROZEN)
    _set_status(db, closed, AccountStatus.CLOSED)
    _hold(db, alice.account_id, bob.account_id, 25_000)

    assert _totals(client, alice) == [
        {
            "currency": "INR",
            "ledger_balance_minor": 157_000,  # 100,000 + 50,000 + 7,000 frozen; closed left out
            "held_minor": 25_000,
            "available_minor": 125_000,  # (100,000 - 25,000 held) + 50,000; frozen adds nothing
            "account_count": 3,
            "frozen_account_count": 1,
        }
    ]
    # Bob's own total is untouched by Alice's accounts, and the hold is not his.
    assert _totals(client, bob) == [
        {
            "currency": "INR",
            "ledger_balance_minor": 999_999,
            "held_minor": 0,
            "available_minor": 999_999,
            "account_count": 1,
            "frozen_account_count": 0,
        }
    ]


def test_a_joint_account_counts_in_full_for_each_holder_only(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=10_000)
    bob = make_customer(funds=40_000)
    carol = make_customer(funds=70_000)
    db.add(
        AccountHolder(
            account_id=bob.account_id, customer_id=alice.customer_id, holder_role=HolderRole.JOINT
        )
    )
    db.commit()

    def ledger_and_count(actor: Actor) -> tuple[object, object]:
        [total] = _totals(client, actor)
        return total["ledger_balance_minor"], total["account_count"]

    assert ledger_and_count(alice) == (50_000, 2)
    assert ledger_and_count(bob) == (40_000, 1)
    assert ledger_and_count(carol) == (70_000, 1)


def test_zero_balance_and_no_counted_accounts(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer()
    assert _totals(client, alice) == [
        {
            "currency": "INR",
            "ledger_balance_minor": 0,
            "held_minor": 0,
            "available_minor": 0,
            "account_count": 1,
            "frozen_account_count": 0,
        }
    ]
    assert alice.account_id is not None
    _set_status(db, alice.account_id, AccountStatus.CLOSED)
    assert _totals(client, alice) == []


def test_summary_is_not_mistaken_for_an_account_id_and_needs_a_customer(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers=alice.headers).status_code == 200
