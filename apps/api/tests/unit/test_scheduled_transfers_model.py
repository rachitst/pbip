"""Focused unit tests for the ScheduledTransfer SQLAlchemy model and metadata registration (TASK-015)."""

import subprocess
import sys

import pytest
from pbip_api.core.db import Base
from pbip_api.modules.payments.models import (
    ScheduledTransfer,
    ScheduledTransferStatus,
    ScheduleFrequency,
    SchedulePauseReason,
)
from sqlalchemy import CheckConstraint, Table

pytestmark = pytest.mark.unit


def _scheduled_transfers_table() -> Table:
    table = ScheduledTransfer.__table__
    assert isinstance(table, Table)
    assert Base.metadata.tables["scheduled_transfers"] is table
    return table


def test_importing_payments_service_registers_scheduled_transfers_table() -> None:
    code = (
        "import pbip_api.modules.payments.service; from pbip_api.core.db import Base; "
        "from sqlalchemy.orm import configure_mappers; configure_mappers(); "
        "Base.metadata.sorted_tables; "
        "assert 'scheduled_transfers' in Base.metadata.tables"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_scheduled_transfer_columns_and_enums() -> None:
    table = _scheduled_transfers_table()
    expected_nullability = {
        "id": False,
        "customer_id": False,
        "user_id": False,
        "from_account_id": False,
        "payee_id": True,
        "to_account_id": True,
        "amount": False,
        "currency": False,
        "reference": True,
        "frequency": False,
        "status": False,
        "start_date": False,
        "end_date": True,
        "next_run_date": False,
        "last_run_date": True,
        "last_transaction_id": True,
        "failure_count": False,
        "last_failure_reason": True,
        "last_failed_occurrence_date": True,
        "pause_reason": True,
        "created_at": False,
    }
    assert len(table.columns) == 21
    assert {col.name: col.nullable for col in table.columns} == expected_nullability
    assert "last_evaluated_date" not in table.columns
    assert getattr(table.columns["currency"].default, "arg", None) == "INR"
    assert getattr(table.columns["status"].default, "arg", None) == ScheduledTransferStatus.ACTIVE
    assert getattr(table.columns["failure_count"].default, "arg", None) == 0
    assert {m.value for m in ScheduleFrequency} == {"DAILY", "WEEKLY", "MONTHLY"}
    assert {m.value for m in ScheduledTransferStatus} == {
        "ACTIVE",
        "PAUSED",
        "CANCELLED",
        "COMPLETED",
    }
    assert {m.value for m in SchedulePauseReason} == {
        "CUSTOMER",
        "FRAUD_BLOCK",
        "CUSTOMER_ACTION_REQUIRED",
    }


def test_scheduled_transfer_check_constraints_and_foreign_keys() -> None:
    table = _scheduled_transfers_table()
    checks = {
        c.name: str(c.sqltext)
        for c in table.constraints
        if isinstance(c, CheckConstraint) and c.name is not None
    }
    assert {
        "ck_scheduled_transfers_amount_positive",
        "ck_scheduled_transfers_failure_count_non_negative",
        "ck_scheduled_transfers_frequency_valid",
        "ck_scheduled_transfers_status_valid",
        "ck_scheduled_transfers_destination_xor",
        "ck_scheduled_transfers_end_date_valid",
        "ck_scheduled_transfers_pause_reason_valid",
    }.issubset(checks.keys())
    assert checks["ck_scheduled_transfers_pause_reason_valid"] == (
        "(status = 'PAUSED' AND pause_reason IS NOT NULL AND pause_reason IN "
        "('CUSTOMER', 'FRAUD_BLOCK', 'CUSTOMER_ACTION_REQUIRED')) "
        "OR (status <> 'PAUSED' AND pause_reason IS NULL)"
    )

    fk_ondelete = {fk.parent.name: fk.ondelete for fk in table.foreign_keys}
    assert fk_ondelete == {
        "customer_id": "RESTRICT",
        "user_id": "RESTRICT",
        "from_account_id": "RESTRICT",
        "payee_id": "RESTRICT",
        "to_account_id": "RESTRICT",
        "last_transaction_id": "SET NULL",
    }


def test_scheduled_transfer_indexes() -> None:
    table = _scheduled_transfers_table()
    indexes = {index.name: [col.name for col in index.columns] for index in table.indexes}
    assert indexes == {
        "ix_scheduled_transfers_status_next_run_date": ["status", "next_run_date"],
        "ix_scheduled_transfers_customer_id_created_at": ["customer_id", "created_at"],
        "ix_scheduled_transfers_user_id": ["user_id"],
        "ix_scheduled_transfers_from_account_id": ["from_account_id"],
        "ix_scheduled_transfers_payee_id": ["payee_id"],
        "ix_scheduled_transfers_to_account_id": ["to_account_id"],
        "ix_scheduled_transfers_last_transaction_id": ["last_transaction_id"],
    }
