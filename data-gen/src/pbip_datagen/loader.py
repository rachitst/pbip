"""Bulk-load a generated dataset into the ``synthetic`` schema with PostgreSQL COPY.

COPY streams rows over one connection without per-row round trips or statement parsing, which is
what makes 1,000 customers (~400k transactions) load in seconds. The load is one transaction:
the run row is replaced (ON DELETE CASCADE removes its old customers/transactions), so loading
the same config twice is idempotent and a failure leaves nothing behind.

This module writes ONLY to the ``synthetic`` schema (ADR 0009); it never touches the operational
ledger tables.
"""

import time
from collections.abc import Iterable
from dataclasses import dataclass
from operator import attrgetter

import psycopg
from psycopg import sql

from pbip_datagen.generator import GENERATOR_VERSION, SyntheticDataset, SyntheticTransaction

_CUSTOMER_COLUMNS = (
    "id",
    "run_id",
    "customer_index",
    "persona",
    "city",
    "monthly_income_minor",
    "savings_rate_bps",
    "has_emi",
    "emi_minor",
    "emi_day",
    "joined_on",
    "churned_on",
)
# Binary COPY skips text formatting/parsing on both sides (~3x faster than text COPY here).
_CUSTOMER_TYPES = ("uuid", "uuid", "int4", "varchar", "varchar", "int8", "int4", "bool", "int8",
                   "int2", "date", "date")  # fmt: skip
_TRANSACTION_TYPES = ("uuid", "uuid", "uuid", "timestamptz", "varchar", "int8", "varchar",
                      "varchar", "varchar")  # fmt: skip
_TRANSACTION_COLUMNS = (
    "id",
    "run_id",
    "customer_id",
    "occurred_at",
    "direction",
    "amount_minor",
    "category",
    "event_type",
    "merchant",
)


@dataclass(frozen=True, slots=True)
class LoadReport:
    customers: int
    transactions: int
    seconds: float


def to_conninfo(database_url: str) -> str:
    """Accept the app's SQLAlchemy URL (``postgresql+psycopg://``) as well as a libpq URL."""
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if database_url.startswith(prefix):
            return "postgresql://" + database_url.removeprefix(prefix)
    return database_url


def in_index_order(transactions: Iterable[SyntheticTransaction]) -> list[SyntheticTransaction]:
    """Rows in ix_transactions_customer_id_occurred_at key order (the table's largest index).

    Index maintenance, not COPY itself, dominates the load: the same rows COPY in ~2.8 s into a
    table without indexes. Inserting in key order appends at the right edge of the B-tree instead
    of splitting pages across it, which cut a fresh 1,000-customer load from a median 13.7 s to
    10.9 s on a 2-core Windows host (#135). Only the order changes: same rows, same ids.
    """
    return sorted(transactions, key=attrgetter("customer_id", "occurred_at"))


def _copy(
    cursor: psycopg.Cursor[tuple[object, ...]],
    table: str,
    columns: Iterable[str],
    types: Iterable[str],
    rows: Iterable[tuple[object, ...]],
) -> None:
    statement = sql.SQL("COPY {}.{} ({}) FROM STDIN (FORMAT BINARY)").format(
        sql.Identifier("synthetic"),
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
    )
    with cursor.copy(statement) as copy:
        copy.set_types(list(types))
        for row in rows:
            copy.write_row(row)


def load_dataset(dataset: SyntheticDataset, database_url: str) -> LoadReport:
    started = time.perf_counter()
    run_id, config = dataset.run_id, dataset.config
    with psycopg.connect(to_conninfo(database_url)) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM synthetic.generation_runs WHERE id = %s", (run_id,))
        cursor.execute(
            "INSERT INTO synthetic.generation_runs "
            "(id, generator_version, seed, customers, months, start_date) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (run_id, GENERATOR_VERSION, config.seed, config.customers, config.months, config.start),
        )
        _copy(
            cursor,
            "customers",
            _CUSTOMER_COLUMNS,
            _CUSTOMER_TYPES,
            (
                (c.id, run_id, c.index, c.persona, c.city, c.monthly_income_minor,
                 c.savings_rate_bps, c.has_emi, c.emi_minor, c.emi_day, c.joined_on, c.churned_on)
                for c in dataset.customers
            ),
        )  # fmt: skip
        # Fresh statistics let the set-based integrity trigger (migration 0002) hash-join the
        # new rows against customers; without them a first load plans a nested loop (~5x slower).
        cursor.execute("ANALYZE synthetic.customers")
        _copy(
            cursor,
            "transactions",
            _TRANSACTION_COLUMNS,
            _TRANSACTION_TYPES,
            (
                (t.id, run_id, t.customer_id, t.occurred_at, t.direction, t.amount_minor,
                 t.category, t.event_type, t.merchant)
                for t in in_index_order(dataset.transactions)
            ),
        )  # fmt: skip
        connection.commit()
    return LoadReport(
        len(dataset.customers), len(dataset.transactions), time.perf_counter() - started
    )
