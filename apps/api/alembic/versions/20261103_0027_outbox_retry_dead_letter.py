"""Outbox relay retries, dead letters, purge index and exactly-once notifications (TASK-026).

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-03

ADR-0040. ``outbox_events`` gains the retry state (``attempts``, ``next_attempt_at``, ``last_error``)
and the dead-letter marker (``dead_lettered_at``). Existing rows start with zero attempts and are due
at their ``created_at``, so pending events keep their order. The poll index now covers
``next_attempt_at`` of pending, not dead-lettered events; a second partial index serves the daily
purge of processed events. ``notifications.outbox_event_id`` (unique, no FK: events are purged, the
notification stays) makes delivery idempotent per event.

Downgrade restores the old poll index and drops the new columns. It loses only operational state:
retry counters, dead-letter markers and the event link on notifications. A dead-lettered event is
still unprocessed after the downgrade, so the old relay picks it up again (nothing is lost).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column("outbox_events", sa.Column("last_error", sa.String(length=100), nullable=True))
    op.add_column(
        "outbox_events", sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Pending events keep their original order: due since they were written.
    op.execute("UPDATE outbox_events SET next_attempt_at = created_at")
    op.create_check_constraint(
        op.f("ck_outbox_events_attempts_non_negative"), "outbox_events", "attempts >= 0"
    )
    op.create_check_constraint(
        op.f("ck_outbox_events_processed_or_dead_lettered"),
        "outbox_events",
        "processed_at IS NULL OR dead_lettered_at IS NULL",
    )
    op.drop_index(
        "ix_outbox_events_unprocessed",
        table_name="outbox_events",
        postgresql_where=sa.text("processed_at IS NULL"),
    )
    op.create_index(
        "ix_outbox_events_due",
        "outbox_events",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL AND dead_lettered_at IS NULL"),
    )
    op.create_index(
        "ix_outbox_events_processed_at",
        "outbox_events",
        ["processed_at"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NOT NULL"),
    )
    op.add_column("notifications", sa.Column("outbox_event_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint(
        op.f("uq_notifications_outbox_event_id"), "notifications", ["outbox_event_id"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_notifications_outbox_event_id"), "notifications", type_="unique")
    op.drop_column("notifications", "outbox_event_id")
    op.drop_index(
        "ix_outbox_events_processed_at",
        table_name="outbox_events",
        postgresql_where=sa.text("processed_at IS NOT NULL"),
    )
    op.drop_index(
        "ix_outbox_events_due",
        table_name="outbox_events",
        postgresql_where=sa.text("processed_at IS NULL AND dead_lettered_at IS NULL"),
    )
    op.create_index(
        "ix_outbox_events_unprocessed",
        "outbox_events",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL"),
    )
    op.drop_constraint(
        op.f("ck_outbox_events_processed_or_dead_lettered"), "outbox_events", type_="check"
    )
    op.drop_constraint(op.f("ck_outbox_events_attempts_non_negative"), "outbox_events", type_="check")
    op.drop_column("outbox_events", "dead_lettered_at")
    op.drop_column("outbox_events", "last_error")
    op.drop_column("outbox_events", "next_attempt_at")
    op.drop_column("outbox_events", "attempts")
