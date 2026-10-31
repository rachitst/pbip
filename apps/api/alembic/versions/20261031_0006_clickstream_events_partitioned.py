"""Create clickstream_events table partitioned monthly by event_timestamp (TASK-055).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-31 10:00:00.000000+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE clickstream_events (
            id UUID NOT NULL,
            event_timestamp TIMESTAMPTZ NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            customer_id UUID REFERENCES customers(id) ON DELETE SET NULL,
            session_id VARCHAR(128) NOT NULL,
            page_url VARCHAR(1024),
            properties JSONB NOT NULL DEFAULT '{}'::jsonb,
            ip_address VARCHAR(64),
            user_agent VARCHAR(512),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (id, event_timestamp)
        ) PARTITION BY RANGE (event_timestamp);
        """
    )

    op.execute(
        """
        CREATE INDEX ix_clickstream_events_customer_timestamp
        ON clickstream_events (customer_id, event_timestamp);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_clickstream_events_session_timestamp
        ON clickstream_events (session_id, event_timestamp);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_clickstream_events_event_type_timestamp
        ON clickstream_events (event_type, event_timestamp);
        """
    )

    partitions = [
        ("clickstream_events_2026_08", "2026-08-01 00:00:00+00", "2026-09-01 00:00:00+00"),
        ("clickstream_events_2026_09", "2026-09-01 00:00:00+00", "2026-10-01 00:00:00+00"),
        ("clickstream_events_2026_10", "2026-10-01 00:00:00+00", "2026-11-01 00:00:00+00"),
        ("clickstream_events_2026_11", "2026-11-01 00:00:00+00", "2026-12-01 00:00:00+00"),
        ("clickstream_events_2026_12", "2026-12-01 00:00:00+00", "2027-01-01 00:00:00+00"),
        ("clickstream_events_2027_01", "2027-01-01 00:00:00+00", "2027-02-01 00:00:00+00"),
        ("clickstream_events_2027_02", "2027-02-01 00:00:00+00", "2027-03-01 00:00:00+00"),
        ("clickstream_events_2027_03", "2027-03-01 00:00:00+00", "2027-04-01 00:00:00+00"),
    ]
    for table_name, from_dt, to_dt in partitions:
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} PARTITION OF clickstream_events
            FOR VALUES FROM ('{from_dt}') TO ('{to_dt}');
            """
        )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS clickstream_events_default
        PARTITION OF clickstream_events DEFAULT;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS clickstream_events CASCADE;")
