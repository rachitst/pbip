"""TASK-016 / UC-03: statements as CSV and PDF, owner-only, with audit and a notification."""

import csv
import io
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.notifications.models import Notification, OutboxEvent
from pbip_api.modules.statements import service as statements
from pbip_api.modules.statements.service import BANK_TZ
from pbip_test_support import Actor, age_payee, count_statements
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def today() -> str:
    return datetime.now(UTC).astimezone(BANK_TZ).date().isoformat()


def get(client: TestClient, actor: Actor, account_id: Any, **params: str) -> Any:
    return client.get(
        f"/api/v1/accounts/{account_id}/statements", headers=actor.headers, params=params
    )


@pytest.fixture
def funded(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> Actor:
    """250,000.00 deposited, then 1,200.50 and 99.50 sent through the real transfer API."""
    alice, bob = make_customer(funds=25_000_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)  # an established payee keeps fraud rules quiet
    for amount in (120_050, 9_950):
        sent = client.post(
            "/api/v1/transfers",
            headers={**alice.headers, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "from_account_id": str(alice.account_id),
                "payee_id": payee["id"],
                "amount_minor": amount,
                "reference": "=HYPERLINK(evil)",
            },
        )
        assert sent.status_code == 201 and sent.json()["status"] == "POSTED", sent.text
    return alice


def test_csv_lists_every_entry_with_running_balance(client: TestClient, funded: Actor) -> None:
    response = get(
        client, funded, funded.account_id, start_date=today(), end_date=today(), format="csv"
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["cache-control"] == "no-store"
    assert 'attachment; filename="pbip-statement-' in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert [(r["debit"], r["credit"], r["balance"]) for r in rows] == [
        ("", "250000.00", "250000.00"),
        ("1200.50", "", "248799.50"),
        ("99.50", "", "248700.00"),
    ]
    # Free text that a spreadsheet would execute is neutralised.
    assert rows[1]["reference"] == "'=HYPERLINK(evil)"


def test_pdf_has_the_bank_header_summary_and_balances(client: TestClient, funded: Actor) -> None:
    response = get(client, funded, funded.account_id, start_date=today(), end_date=today())
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    body = response.content
    assert body.startswith(b"%PDF-1.4") and body.rstrip().endswith(b"%%EOF")
    for text in (
        b"PBIP Bank",
        b"Account statement",
        b"Opening balance",
        b"INR 0.00",
        b"Closing balance",
        b"INR 2,48,700.00",
        b"Money out",
        b"INR 1,300.00",
    ):
        assert text in body, text


def test_opening_balance_carries_history_before_the_period(
    client: TestClient, funded: Actor, migrated_engine: Engine
) -> None:
    """A period after all activity opens and closes at the current balance with no lines."""
    with Session(migrated_engine) as session:
        account = session.get(Account, funded.account_id)
        assert account is not None
        later = datetime.now(UTC) + timedelta(days=2)
        day = later.astimezone(BANK_TZ).date()
        statement = statements.build_statement(session, account, "Test Customer", day, day, later)
    assert statement.lines == []
    assert statement.opening == statement.closing == 24_870_000


def test_download_is_audited_and_notifies_without_amounts(
    client: TestClient, funded: Actor, migrated_engine: Engine
) -> None:
    assert (
        get(client, funded, funded.account_id, start_date=today(), end_date=today()).status_code
        == 200
    )
    with Session(migrated_engine) as session:
        audit = session.execute(
            select(AuditLog).where(
                AuditLog.action == "STATEMENT_DOWNLOADED",
                AuditLog.resource_id == str(funded.account_id),
            )
        ).scalar_one()
        assert audit.details == {"start": today(), "end": today(), "format": "pdf", "entries": 3}
        event = session.execute(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "STATEMENT_DOWNLOADED",
                OutboxEvent.payload["account_id"].astext == str(funded.account_id),
            )
        ).scalar_one()
        assert set(event.payload) == {
            "user_id",
            "account_id",
            "account_masked",
            "start",
            "end",
            "format",
        }


def test_another_customer_gets_404_and_nothing_is_recorded(
    client: TestClient,
    funded: Actor,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
) -> None:
    mallory = make_customer()
    for account_id in (funded.account_id, uuid.uuid4()):
        response = get(client, mallory, account_id, start_date=today(), end_date=today())
        assert response.status_code == 404
    with Session(migrated_engine) as session:
        assert (
            session.execute(
                select(AuditLog).where(
                    AuditLog.action == "STATEMENT_DOWNLOADED",
                    AuditLog.resource_id == str(funded.account_id),
                )
            ).first()
            is None
        )


@pytest.mark.parametrize(
    "params",
    [
        {"start_date": "2026-02-10", "end_date": "2026-02-01"},
        {"start_date": "2024-01-01", "end_date": "2025-06-01"},
        {"start_date": "2999-01-01", "end_date": "2999-01-02"},
        {"start_date": "not-a-date", "end_date": "2026-01-01"},
        {"start_date": "2026-01-01", "end_date": "2026-01-02", "format": "xlsx"},
        {"start_date": "2026-01-01"},
    ],
)
def test_invalid_periods_and_formats_are_rejected(
    client: TestClient, funded: Actor, params: dict[str, str]
) -> None:
    assert get(client, funded, funded.account_id, **params).status_code == 422


def test_a_statement_needs_authentication(client: TestClient, funded: Actor) -> None:
    response = client.get(
        f"/api/v1/accounts/{funded.account_id}/statements",
        params={"start_date": today(), "end_date": today()},
    )
    assert response.status_code == 401


def _statement_queries(session: Session, account: Account, now: datetime) -> tuple[int, int]:
    """(lines, SQL statements) for building and rendering today's statement as CSV and PDF."""
    day = now.astimezone(BANK_TZ).date()
    bind = session.get_bind()
    assert isinstance(bind, Engine)
    with count_statements(bind) as sent:
        statement = statements.build_statement(session, account, "Test Customer", day, day, now)
        statements.render_csv(statement)
        statements.render_pdf(statement)
    return len(statement.lines), len(sent)


def test_one_thousand_transactions_cost_the_same_queries_as_ten(
    make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    """TASK-016: a 1,000-entry statement (query, build, CSV and PDF) is a fixed number of queries.

    The cost is asserted as the SQL statement count, the same with 10 entries as with 1,000, not
    as a wall-clock bound: a timing limit failed on loaded and Windows machines (#317) and says
    nothing about this code. A per-line query (N+1) makes the counts differ. The query itself is
    an index scan on ix_transaction_entries_account_id_created_at (~5 ms, PR #302).
    """
    alice = make_customer()
    with Session(migrated_engine) as session:
        for _ in range(10):
            ledger.deposit(session, alice.account_id, 1_00)
        session.commit()
        account = session.get(Account, alice.account_id)
        assert account is not None
        small_lines, small_queries = _statement_queries(session, account, datetime.now(UTC))
        for _ in range(990):
            ledger.deposit(session, alice.account_id, 1_00)
        session.commit()
        session.expire_all()
        account = session.get(Account, alice.account_id)
        assert account is not None
        lines, queries = _statement_queries(session, account, datetime.now(UTC))
    assert (small_lines, lines) == (10, 1_000)
    assert queries == small_queries, "statement queries grow with the number of entries (N+1)"
    assert queries <= 5, queries


def test_the_bell_says_a_statement_was_downloaded(
    client: TestClient, funded: Actor, migrated_engine: Engine
) -> None:
    assert (
        get(
            client, funded, funded.account_id, start_date=today(), end_date=today(), format="csv"
        ).status_code
        == 200
    )
    with Session(migrated_engine) as session:
        notifications.relay_outbox(session)
        session.commit()
        note = session.execute(
            select(Notification).where(
                Notification.user_id == funded.user_id,
                Notification.type == "STATEMENT_DOWNLOADED",
            )
        ).scalar_one()
    assert note.title == "Statement downloaded"
    assert note.body.startswith("A CSV statement for account ")
    assert f"({today()} to {today()}) was downloaded" in note.body
    assert note.account_id == funded.account_id and note.resource_type == "security"
