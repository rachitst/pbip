"""Time-windowed per-account reads (spending breakdown, trends, health score) need an index that
leads with account_id and then created_at; (account_id, seq) does not serve a created_at range.
The migration and the model must agree (CI runs `alembic check`)."""

import pytest
from pbip_api.modules.ledger.models import TransactionEntry

pytestmark = pytest.mark.unit


def test_entries_are_indexed_by_account_then_time() -> None:
    indexes = {
        index.name: [column.name for column in index.columns]
        for index in TransactionEntry.__table__.indexes
    }
    assert indexes.get("ix_transaction_entries_account_id_created_at") == [
        "account_id",
        "created_at",
    ]
