"""TASK-017: filtering and searching an account's transactions with the keyset cursor."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.calendar import bank_today
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.service import EntryFilters
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def entries(client: TestClient, actor: Actor, **params: Any) -> Any:
    return client.get(
        f"/api/v1/accounts/{actor.account_id}/transactions", headers=actor.headers, params=params
    )


@pytest.fixture
def history(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> Actor:
    """Credits: 500.00 'Salary October', 20.00 'Refund 100% off_promo'; debits: 75.25 'Rent'."""
    alice, bob = make_customer(), make_customer()
    with Session(migrated_engine) as session:
        ledger.deposit(session, alice.account_id, 50_000, reference="Salary October")
        ledger.deposit(session, alice.account_id, 2_000, reference="Refund 100% off_promo")
        session.commit()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    sent = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "from_account_id": str(alice.account_id),
            "payee_id": payee["id"],
            "amount_minor": 7_525,
            "reference": "Rent",
        },
    )
    assert sent.status_code == 201, sent.text
    return alice


def refs(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["reference"] for item in response.json()["items"]]


def test_amount_direction_and_text_filters(client: TestClient, history: Actor) -> None:
    assert refs(entries(client, history, min_amount=5_000)) == ["Rent", "Salary October"]
    assert refs(entries(client, history, max_amount=7_525)) == ["Rent", "Refund 100% off_promo"]
    assert refs(entries(client, history, type="DEBIT")) == ["Rent"]
    assert refs(entries(client, history, type="CREDIT", search="salary")) == ["Salary October"]
    # LIKE wildcards in the search are literal text, not patterns.
    assert refs(entries(client, history, search="100%")) == ["Refund 100% off_promo"]
    assert refs(entries(client, history, search="off_p")) == ["Refund 100% off_promo"]
    # Would match "Salary" if "_" and "%" were wildcards:
    assert refs(entries(client, history, search="Sal_ry")) == []
    assert refs(entries(client, history, search="Sal%ober")) == []
    assert refs(entries(client, history, search="%")) == ["Refund 100% off_promo"]


def test_date_range_is_bank_days_inclusive(client: TestClient, history: Actor) -> None:
    today = bank_today(datetime.now(UTC))
    day = today.isoformat()
    assert len(refs(entries(client, history, start_date=day, end_date=day))) == 3
    yesterday = (today - timedelta(days=1)).isoformat()
    assert refs(entries(client, history, end_date=yesterday)) == []
    tomorrow = (today + timedelta(days=1)).isoformat()
    assert refs(entries(client, history, start_date=tomorrow)) == []


def test_cursor_walks_the_filtered_list_without_gaps_or_repeats(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer()
    with Session(migrated_engine) as session:
        for i in range(50):
            amount = 200 if i % 2 else 100
            ledger.deposit(session, alice.account_id, amount, reference=f"d{i}")
        session.commit()
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"min_amount": 200, "limit": 10}
        if cursor:
            params["cursor"] = cursor
        page = entries(client, alice, **params).json()
        assert all(item["amount_minor"] == 200 for item in page["items"])
        seen += [item["reference"] for item in page["items"]]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert seen == [f"d{i}" for i in range(49, 0, -2)]


@pytest.mark.parametrize(
    "params",
    [
        {"min_amount": 500, "max_amount": 100},
        {"start_date": "2026-02-10", "end_date": "2026-02-01"},
        {"min_amount": 0},
        {"max_amount": -5},
        {"min_amount": "1.5"},
        {"type": "SIDEWAYS"},
        {"start_date": "yesterday"},
        {"search": "x" * 65},
        {"search": "bad\x00text"},
        {"search": ""},
    ],
)
def test_invalid_filters_are_rejected(
    client: TestClient, history: Actor, params: dict[str, Any]
) -> None:
    assert entries(client, history, **params).status_code == 422


def test_sql_in_the_search_is_just_text(
    client: TestClient, history: Actor, migrated_engine: Engine
) -> None:
    attack = "'; DROP TABLE transaction_entries; --"
    assert refs(entries(client, history, search=attack)) == []
    with Session(migrated_engine) as session:
        assert session.execute(text("SELECT count(*) FROM transaction_entries")).scalar_one() > 0


def test_another_customer_cannot_search_my_account(
    client: TestClient, history: Actor, make_customer: Callable[..., Actor]
) -> None:
    mallory = make_customer()
    response = client.get(
        f"/api/v1/accounts/{history.account_id}/transactions",
        headers=mallory.headers,
        params={"search": "Salary"},
    )
    assert response.status_code == 404


def test_filtered_query_uses_an_account_index(history: Actor, migrated_engine: Engine) -> None:
    """The plan reads the account's rows by index, never the whole entries table."""
    captured: list[tuple[str, Any]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, params: Any, *_: Any) -> None:
        if "FROM transaction_entries JOIN transactions" in statement:
            captured.append((statement, params))

    event.listen(migrated_engine, "before_cursor_execute", capture)
    try:
        with Session(migrated_engine) as session:
            today = datetime.now(UTC)
            ledger.list_account_entries(
                session,
                history.account_id,
                26,
                None,
                filters=EntryFilters(start=today - timedelta(days=30), end=today, min_amount=1),
            )
    finally:
        event.remove(migrated_engine, "before_cursor_execute", capture)
    assert len(captured) == 1
    statement, params = captured[0]
    with migrated_engine.connect() as conn:
        conn.exec_driver_sql("SET enable_seqscan = off")  # tiny test tables would favour a scan
        plan = "\n".join(
            row[0] for row in conn.exec_driver_sql(f"EXPLAIN {statement}", params).all()
        )
        conn.rollback()
    assert "ix_transaction_entries_account_id" in plan, plan
    assert "Seq Scan on transaction_entries" not in plan, plan
