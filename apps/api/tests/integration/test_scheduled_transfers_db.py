"""PostgreSQL schema, CHECK constraint, FK, and advisory-lock tests for scheduled_transfers (TASK-015)."""

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from pbip_api.modules.payments.models import (
    ScheduledTransfer,
    ScheduledTransferStatus,
    ScheduleFrequency,
    SchedulePauseReason,
)
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration


def _valid_row(alice: Any, bob: Any) -> dict[str, Any]:
    return {
        "customer_id": alice.customer_id,
        "user_id": alice.user_id,
        "from_account_id": alice.account_id,
        "to_account_id": bob.account_id,
        "payee_id": None,
        "amount": 10_000,
        "currency": "INR",
        "reference": "Standing order",
        "frequency": ScheduleFrequency.MONTHLY,
        "status": ScheduledTransferStatus.ACTIVE,
        "start_date": date(2026, 11, 1),
        "end_date": date(2027, 11, 1),
        "next_run_date": date(2026, 11, 1),
    }


def test_scheduled_transfer_persists_valid_row_and_enforces_check_constraints(
    db: Session,
    make_customer: Callable[..., Any],
    make_verified_payee: Callable[..., dict[str, Any]],
) -> None:
    alice, bob = make_customer(funds=50_000), make_customer()
    payee = make_verified_payee(alice, bob)

    row = ScheduledTransfer(**{**_valid_row(alice, bob), "reference": None})
    db.add(row)
    db.commit()
    assert row.id is not None
    assert row.currency == "INR"
    assert row.reference is None
    assert row.failure_count == 0
    assert row.last_failed_occurrence_date is None
    assert row.status == ScheduledTransferStatus.ACTIVE
    assert row.pause_reason is None
    assert row.created_at is not None

    invalid_overrides: list[dict[str, Any]] = [
        {"amount": 0},  # ck_scheduled_transfers_amount_positive
        {"amount": -100},  # ck_scheduled_transfers_amount_positive
        {"failure_count": -1},  # ck_scheduled_transfers_failure_count_non_negative
        {"to_account_id": None, "payee_id": None},  # ck_scheduled_transfers_destination_xor
        {
            "to_account_id": bob.account_id,
            "payee_id": payee["id"],
        },  # ck_scheduled_transfers_destination_xor
        {
            "start_date": date(2026, 11, 10),
            "end_date": date(2026, 11, 5),
        },  # ck_scheduled_transfers_end_date_valid
        {
            "status": ScheduledTransferStatus.PAUSED,
            "pause_reason": "UNKNOWN_REASON",
        },  # ck_scheduled_transfers_pause_reason_valid (invalid reason)
        {
            "status": ScheduledTransferStatus.PAUSED,
            "pause_reason": None,
        },  # ck_scheduled_transfers_pause_reason_valid (paused without reason)
        {
            "status": ScheduledTransferStatus.ACTIVE,
            "pause_reason": SchedulePauseReason.CUSTOMER.value,
        },  # ck_scheduled_transfers_pause_reason_valid (active with reason)
        {
            "status": ScheduledTransferStatus.CANCELLED,
            "pause_reason": SchedulePauseReason.FRAUD_BLOCK.value,
        },  # ck_scheduled_transfers_pause_reason_valid (cancelled with reason)
        {
            "status": ScheduledTransferStatus.COMPLETED,
            "pause_reason": SchedulePauseReason.CUSTOMER_ACTION_REQUIRED.value,
        },  # ck_scheduled_transfers_pause_reason_valid (completed with reason)
    ]
    for override in invalid_overrides:
        bad = ScheduledTransfer(**{**_valid_row(alice, bob), **override})
        db.add(bad)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    for valid_reason in (
        SchedulePauseReason.CUSTOMER.value,
        SchedulePauseReason.FRAUD_BLOCK.value,
        SchedulePauseReason.CUSTOMER_ACTION_REQUIRED.value,
    ):
        paused = ScheduledTransfer(
            **{
                **_valid_row(alice, bob),
                "status": ScheduledTransferStatus.PAUSED,
                "pause_reason": valid_reason,
            }
        )
        db.add(paused)
        db.flush()
        db.rollback()

    for terminal_status in (
        ScheduledTransferStatus.CANCELLED,
        ScheduledTransferStatus.COMPLETED,
    ):
        terminal = ScheduledTransfer(
            **{
                **_valid_row(alice, bob),
                "status": terminal_status,
                "pause_reason": None,
            }
        )
        db.add(terminal)
        db.flush()
        db.rollback()


def test_advisory_lock_two_int_and_single_bigint_keyspaces_never_collide(
    migrated_engine: Engine,
) -> None:
    """ADR-0021 R9: prove in real PostgreSQL that (int4, int4) and (bigint) locks do not collide."""
    namespace = 1346521424  # 0x50424950 ("PBIP")
    key_lo = 42
    combined_bigint = (namespace << 32) | key_lo

    factory = sessionmaker(bind=migrated_engine)
    with factory() as s1, factory() as s2:
        # Session 1 holds the single-bigint advisory lock with the exact same 64-bit bit pattern.
        s1.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": combined_bigint})

        # Session 2 must still acquire the two-int advisory lock (objsubid=2 vs objsubid=1).
        acquired = s2.execute(
            text("SELECT pg_try_advisory_xact_lock(:ns, :id)"),
            {"ns": namespace, "id": key_lo},
        ).scalar_one()
        assert acquired is True

        # Session 1 trying the SAME two-int lock while Session 2 holds it must get False.
        contended = s1.execute(
            text("SELECT pg_try_advisory_xact_lock(:ns, :id)"),
            {"ns": namespace, "id": key_lo},
        ).scalar_one()
        assert contended is False
        s1.rollback()
        s2.rollback()
