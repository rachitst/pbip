"""TASK-008: keyset (cursor) pagination of an account's ledger entries.

The properties are checked against real PostgreSQL: walking every page returns each entry exactly
once in posting order, and entries written while a client is paging neither duplicate nor drop
rows from the walk.
"""

import threading
import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pbip_api.core.pagination import PageDirection
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.accounts.service import list_entries_page
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import TransactionEntry
from pbip_test_support import Actor
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration


def _deposit(engine: Engine, account_id: UUID, count: int) -> None:
    with sessionmaker(bind=engine)() as session:
        for i in range(count):
            ledger.deposit(session, account_id, 100 + i)
            session.commit()


def _all_seqs(engine: Engine, account_id: UUID) -> list[int]:
    with sessionmaker(bind=engine)() as session:
        return list(
            session.execute(
                select(TransactionEntry.seq)
                .where(TransactionEntry.account_id == account_id)
                .order_by(TransactionEntry.seq)
            ).scalars()
        )


def _walk(
    engine: Engine,
    account_id: UUID,
    limit: int,
    direction: PageDirection,
    between_pages: Callable[[], None] | None = None,
) -> list[int]:
    seen: list[int] = []
    cursor: str | None = None
    while True:
        with sessionmaker(bind=engine)() as session:
            account = session.get_one(Account, account_id)
            page = list_entries_page(session, account, limit, cursor, direction)
        assert len(page.items) <= limit
        assert page.has_more == (page.next_cursor is not None)
        seen.extend(item.entry_seq for item in page.items)
        if not page.has_more:
            return seen
        cursor = page.next_cursor
        if between_pages is not None:
            between_pages()


# ------------------------------------------------------------------- HTTP contract


def _page(client: TestClient, actor: Actor, **params: Any) -> dict[str, Any]:
    response = client.get(
        f"/api/v1/accounts/{actor.account_id}/transactions", headers=actor.headers, params=params
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_pages_expose_items_next_cursor_and_has_more(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer()
    assert alice.account_id is not None
    _deposit(migrated_engine, alice.account_id, 5)

    first = _page(client, alice, limit=2)
    assert first["has_more"] is True and isinstance(first["next_cursor"], str)
    assert [i["amount_minor"] for i in first["items"]] == [104, 103]  # newest first
    second = _page(client, alice, limit=2, cursor=first["next_cursor"])
    third = _page(client, alice, limit=2, cursor=second["next_cursor"])
    assert [i["amount_minor"] for i in second["items"] + third["items"]] == [102, 101, 100]
    assert third["has_more"] is False and third["next_cursor"] is None


def test_newer_direction_walks_forward_from_a_cursor(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer()
    assert alice.account_id is not None
    _deposit(migrated_engine, alice.account_id, 4)
    oldest_first = _page(client, alice, limit=1, direction="newer")
    assert [i["amount_minor"] for i in oldest_first["items"]] == [100]
    rest = _page(client, alice, limit=10, direction="newer", cursor=oldest_first["next_cursor"])
    assert [i["amount_minor"] for i in rest["items"]] == [101, 102, 103]
    assert rest["has_more"] is False


def test_legacy_integer_cursor_keeps_working(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer()
    assert alice.account_id is not None
    _deposit(migrated_engine, alice.account_id, 3)
    newest = _page(client, alice, limit=1)["items"][0]
    older = _page(client, alice, limit=10, cursor=str(newest["entry_seq"]))
    assert [i["amount_minor"] for i in older["items"]] == [101, 100]


def test_empty_account_returns_an_empty_last_page(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    page = _page(client, make_customer())
    assert page == {"items": [], "next_cursor": None, "has_more": False}


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "garbage!!"},
        {"cursor": "0"},
        {"cursor": "x" * 201},
        {"direction": "sideways"},
        {"limit": 0},
        {"limit": 101},
    ],
)
def test_invalid_parameters_are_rejected_with_422(
    client: TestClient, make_customer: Callable[..., Actor], params: dict[str, Any]
) -> None:
    alice = make_customer()
    response = client.get(
        f"/api/v1/accounts/{alice.account_id}/transactions", headers=alice.headers, params=params
    )
    assert response.status_code == 422
    assert "garbage" not in response.text  # errors never echo input


@pytest.mark.security
def test_ownership_is_checked_before_any_cursor_is_used(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice, bob = make_customer(), make_customer()
    assert alice.account_id is not None
    _deposit(migrated_engine, alice.account_id, 2)
    cursor = _page(client, alice, limit=1)["next_cursor"]
    for account_id in (alice.account_id, uuid.uuid4()):
        response = client.get(
            f"/api/v1/accounts/{account_id}/transactions",
            headers=bob.headers,
            params={"cursor": cursor},
        )
        assert response.status_code == 404  # foreign == non-existent
    bad_cursor_foreign = client.get(
        f"/api/v1/accounts/{alice.account_id}/transactions",
        headers=bob.headers,
        params={"cursor": "garbage!!"},
    )
    assert bad_cursor_foreign.status_code == 404  # 404 wins: no oracle via cursor validation


# ------------------------------------------------------------ properties (Hypothesis)

_db_settings = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture
def account_id(make_customer: Callable[..., Actor]) -> UUID:
    actor = make_customer()
    assert actor.account_id is not None
    return actor.account_id


@_db_settings
@given(new_entries=st.integers(min_value=0, max_value=8), limit=st.integers(1, 6))
def test_walking_all_pages_yields_every_entry_once_in_order(
    migrated_engine: Engine, account_id: UUID, new_entries: int, limit: int
) -> None:
    _deposit(migrated_engine, account_id, new_entries)
    expected = _all_seqs(migrated_engine, account_id)
    assert _walk(migrated_engine, account_id, limit, PageDirection.OLDER) == expected[::-1]
    assert _walk(migrated_engine, account_id, limit, PageDirection.NEWER) == expected


@settings(_db_settings, max_examples=12)  # each example walks the account twice with writes
@given(
    initial=st.integers(min_value=1, max_value=8),
    writes_per_page=st.integers(min_value=1, max_value=3),
    limit=st.integers(1, 4),
)
def test_writes_between_pages_never_duplicate_or_drop_rows(
    migrated_engine: Engine, account_id: UUID, initial: int, writes_per_page: int, limit: int
) -> None:
    _deposit(migrated_engine, account_id, initial)
    snapshot = _all_seqs(migrated_engine, account_id)
    boundaries = {"left": 0}

    def write() -> None:
        # Write at the first three page boundaries only: a forward walk that keeps receiving
        # more rows than it reads per page would (correctly) never reach the end.
        if boundaries["left"] > 0:
            boundaries["left"] -= 1
            _deposit(migrated_engine, account_id, writes_per_page)

    # Walking back: rows posted mid-walk are newer than the first page, so the walk is exactly
    # the snapshot (they appear on the client's next refresh, not as duplicates).
    boundaries["left"] = 3
    older = _walk(migrated_engine, account_id, limit, PageDirection.OLDER, between_pages=write)
    assert older == snapshot[::-1]

    # Walking forward: rows posted mid-walk are picked up at the end, each exactly once.
    boundaries["left"] = 3
    newer = _walk(migrated_engine, account_id, limit, PageDirection.NEWER, between_pages=write)
    final = _all_seqs(migrated_engine, account_id)
    assert newer == final
    assert len(set(newer)) == len(newer)


def test_paging_back_is_stable_while_another_thread_keeps_posting(
    migrated_engine: Engine, account_id: UUID
) -> None:
    _deposit(migrated_engine, account_id, 30)
    snapshot = _all_seqs(migrated_engine, account_id)
    stop = threading.Event()

    def writer() -> None:
        while not stop.is_set():
            _deposit(migrated_engine, account_id, 1)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        walked = _walk(migrated_engine, account_id, 4, PageDirection.OLDER)
    finally:
        stop.set()
        thread.join(timeout=10)
    # The first page may already include rows the writer committed after the snapshot; every
    # row at or below the snapshot's newest seq must appear exactly once, newest first.
    assert walked == sorted(walked, reverse=True)
    assert len(set(walked)) == len(walked)
    assert [s for s in walked if s <= snapshot[-1]] == snapshot[::-1]


def test_entry_page_is_built_without_mutating_the_session(
    db: Session, migrated_engine: Engine, account_id: UUID
) -> None:
    _deposit(migrated_engine, account_id, 2)
    account = db.get_one(Account, account_id)
    list_entries_page(db, account, 1, None, PageDirection.OLDER)
    assert not db.dirty and not db.new
