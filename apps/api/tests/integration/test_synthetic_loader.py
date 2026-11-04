"""TASK-009: COPY bulk load of the synthetic dataset and the schema's DB-level integrity rules."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from pbip_datagen.generator import GeneratorConfig, SyntheticDataset, generate
from pbip_datagen.loader import load_dataset
from psycopg import sql
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


@pytest.fixture
def synthetic(migrated_engine: Engine) -> Iterator[Engine]:
    def wipe() -> None:
        with migrated_engine.begin() as connection:
            connection.execute(text("DELETE FROM synthetic.generation_runs"))

    wipe()
    yield migrated_engine
    wipe()


def _scalar(engine: Engine, sql: str, **params: object) -> object:
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _load_and_record(
    dataset: SyntheticDataset, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Load ``dataset``; return each statement the loader's psycopg cursor sent, COPYs included."""
    sent: list[str] = []
    execute, copy = psycopg.Cursor.execute, psycopg.Cursor.copy

    def text_of(cursor: psycopg.Cursor[Any], query: Any) -> str:
        return query.as_string(cursor) if isinstance(query, sql.Composable) else str(query)

    def spy_execute(cursor: psycopg.Cursor[Any], query: Any, *args: Any, **kwargs: Any) -> Any:
        sent.append(text_of(cursor, query))
        return execute(cursor, query, *args, **kwargs)

    def spy_copy(cursor: psycopg.Cursor[Any], statement: Any, *args: Any, **kwargs: Any) -> Any:
        sent.append(text_of(cursor, statement))
        return copy(cursor, statement, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(psycopg.Cursor, "execute", spy_execute)
        patched.setattr(psycopg.Cursor, "copy", spy_copy)
        report = load_dataset(dataset, database_url)
    assert (report.customers, report.transactions) == (
        len(dataset.customers),
        len(dataset.transactions),
    )
    return sent


# Hang guard, not a speed bar (as #362): generating and loading ~400k rows takes 15-25 s on a
# 2-CPU Windows laptop (#135), so 60 s left little margin under load.
@pytest.mark.timeout(300)
def test_copy_loads_1000_customers_with_one_copy_per_table(
    synthetic: Engine, test_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0009's 15 s budget rests on the load's shape, asserted here as cost, not time.

    1,000 customers (~400k transactions) are sent as the same five statements as 10: one COPY per
    table (no per-row INSERT), ANALYZE before the transactions COPY (the integrity trigger then
    hash-joins, #135) and a statement-level, set-based integrity trigger (a row-level one would run
    once per row). Row counts and the amount total are exact. A wall-clock bound failed on 2-CPU
    laptops (#135); the real time is measured by scripts/benchmark_synthetic_load.py.
    """
    small = _load_and_record(
        generate(GeneratorConfig(seed=41, customers=10, months=12)), test_database_url, monkeypatch
    )
    dataset = generate(GeneratorConfig(seed=42, customers=1000, months=12))
    sent = _load_and_record(dataset, test_database_url, monkeypatch)

    assert sent == small, "the loader's statements grow with the dataset"
    assert [statement.split(" (")[0].replace('"', "") for statement in sent] == [
        "DELETE FROM synthetic.generation_runs WHERE id = %s",
        "INSERT INTO synthetic.generation_runs",
        "COPY synthetic.customers",
        "ANALYZE synthetic.customers",
        "COPY synthetic.transactions",
    ]
    assert all(s.endswith("FROM STDIN (FORMAT BINARY)") for s in sent if s.startswith("COPY"))
    row_triggers = _scalar(
        synthetic,
        "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'synthetic.transactions'::regclass "
        "AND NOT tgisinternal AND tgtype & 4 = 4 AND tgtype & 1 = 1",  # INSERT, FOR EACH ROW
    )
    assert row_triggers == 0
    run = {"run": dataset.run_id}
    count = "SELECT count(*) FROM synthetic.{} WHERE run_id = :run"
    assert _scalar(synthetic, count.format("customers"), **run) == 1000
    assert _scalar(synthetic, count.format("transactions"), **run) == len(dataset.transactions)
    loaded_total = _scalar(
        synthetic, "SELECT sum(amount_minor) FROM synthetic.transactions WHERE run_id = :run", **run
    )
    assert loaded_total == sum(t.amount_minor for t in dataset.transactions)
    # The operational ledger is never touched by the synthetic loader (ADR 0009).
    assert _scalar(synthetic, "SELECT count(*) FROM transaction_entries") == 0


def test_reloading_a_run_is_idempotent_and_runs_coexist(
    synthetic: Engine, test_database_url: str
) -> None:
    first = generate(GeneratorConfig(seed=1, customers=20, months=2))
    other = generate(GeneratorConfig(seed=2, customers=10, months=2))
    load_dataset(first, test_database_url)
    load_dataset(first, test_database_url)
    load_dataset(other, test_database_url)

    assert _scalar(synthetic, "SELECT count(*) FROM synthetic.generation_runs") == 2
    assert _scalar(synthetic, "SELECT count(*) FROM synthetic.customers") == 30
    expected = len(first.transactions) + len(other.transactions)
    assert _scalar(synthetic, "SELECT count(*) FROM synthetic.transactions") == expected
    seed = _scalar(
        synthetic, "SELECT seed FROM synthetic.generation_runs WHERE id = :id", id=first.run_id
    )
    assert seed == 1


def test_loaded_rows_round_trip_exactly(synthetic: Engine, test_database_url: str) -> None:
    dataset = generate(GeneratorConfig(seed=5, customers=3, months=2))
    load_dataset(dataset, test_database_url)
    expected = dataset.transactions[0]
    with synthetic.connect() as connection:
        row = connection.execute(
            text(
                "SELECT customer_id, occurred_at, direction, amount_minor, event_type, merchant "
                "FROM synthetic.transactions WHERE id = :id"
            ),
            {"id": expected.id},
        ).one()
    assert row.customer_id == expected.customer_id
    assert row.occurred_at == expected.occurred_at  # same instant, timezone preserved
    assert (row.direction, row.amount_minor) == (expected.direction, expected.amount_minor)
    assert (row.event_type, row.merchant) == (expected.event_type, expected.merchant)


def _insert_transaction(engine: Engine, run_id: uuid.UUID, customer_id: uuid.UUID) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO synthetic.transactions (id, run_id, customer_id, occurred_at, "
                "direction, amount_minor, category, event_type, merchant) VALUES "
                "(:id, :run, :customer, :at, 'DEBIT', 100, 'dining', 'DISCRETIONARY', 'X')"
            ),
            {"id": uuid.uuid4(), "run": run_id, "customer": customer_id, "at": datetime.now(UTC)},
        )


def test_database_rejects_orphan_and_cross_run_transactions(
    synthetic: Engine, test_database_url: str
) -> None:
    a = generate(GeneratorConfig(seed=1, customers=2, months=1))
    b = generate(GeneratorConfig(seed=2, customers=2, months=1))
    load_dataset(a, test_database_url)
    load_dataset(b, test_database_url)

    with pytest.raises(DBAPIError, match="outside its run"):
        _insert_transaction(synthetic, a.run_id, uuid.uuid4())  # no such customer
    with pytest.raises(DBAPIError, match="outside its run"):
        _insert_transaction(synthetic, a.run_id, b.customers[0].id)  # customer of another run
    _insert_transaction(synthetic, a.run_id, a.customers[0].id)  # consistent row is accepted


def test_rows_are_immutable(synthetic: Engine, test_database_url: str) -> None:
    load_dataset(generate(GeneratorConfig(seed=3, customers=2, months=1)), test_database_url)
    for statement in (
        "UPDATE synthetic.transactions SET amount_minor = amount_minor + 1",
        "UPDATE synthetic.customers SET city = 'Elsewhere'",
    ):
        with pytest.raises(DBAPIError, match="immutable"), synthetic.begin() as connection:
            connection.execute(text(statement))


def test_deleting_a_run_cascades_to_customers_and_transactions(
    synthetic: Engine, test_database_url: str
) -> None:
    keep = generate(GeneratorConfig(seed=4, customers=2, months=1))
    drop = generate(GeneratorConfig(seed=6, customers=3, months=1))
    load_dataset(keep, test_database_url)
    load_dataset(drop, test_database_url)
    with synthetic.begin() as connection:
        connection.execute(
            text("DELETE FROM synthetic.generation_runs WHERE id = :id"), {"id": drop.run_id}
        )
    assert _scalar(synthetic, "SELECT count(*) FROM synthetic.customers") == 2
    remaining = _scalar(synthetic, "SELECT count(*) FROM synthetic.transactions")
    assert remaining == len(keep.transactions)


def test_truncating_customers_while_transactions_exist_is_refused(
    synthetic: Engine, test_database_url: str
) -> None:
    load_dataset(generate(GeneratorConfig(seed=8, customers=1, months=1)), test_database_url)
    refused = pytest.raises(DBAPIError, match=r"truncate synthetic\.transactions")
    with refused, synthetic.begin() as connection:
        connection.execute(text("TRUNCATE synthetic.customers CASCADE"))


def test_concurrent_run_deletion_cannot_orphan_uncommitted_transactions(
    synthetic: Engine, test_database_url: str
) -> None:
    """Review finding: the set-based check took no locks, so deleting a run while another
    transaction inserted rows for its customers could commit orphans. The insert trigger now
    takes KEY SHARE locks on the referenced customers, so the delete has to wait."""
    dataset = generate(GeneratorConfig(seed=11, customers=1, months=1))
    load_dataset(dataset, test_database_url)
    customer = dataset.customers[0]

    inserter = synthetic.connect()
    deleter = synthetic.connect()
    try:
        inserter.begin()
        inserter.execute(
            text(
                "INSERT INTO synthetic.transactions (id, run_id, customer_id, occurred_at, "
                "direction, amount_minor, category, event_type, merchant) VALUES "
                "(:id, :run, :customer, now(), 'DEBIT', 100, 'dining', 'DISCRETIONARY', 'X')"
            ),
            {"id": uuid.uuid4(), "run": dataset.run_id, "customer": customer.id},
        )  # uncommitted: holds KEY SHARE on the customer row

        deleter.begin()
        deleter.execute(text("SET LOCAL lock_timeout = '500ms'"))
        with pytest.raises(DBAPIError, match="lock timeout"):
            deleter.execute(
                text("DELETE FROM synthetic.generation_runs WHERE id = :id"),
                {"id": dataset.run_id},
            )
        deleter.rollback()
        inserter.commit()
    finally:
        inserter.close()
        deleter.close()

    with synthetic.connect() as connection:
        orphans = connection.execute(
            text(
                "SELECT count(*) FROM synthetic.transactions t "
                "LEFT JOIN synthetic.customers c ON c.id = t.customer_id WHERE c.id IS NULL"
            )
        ).scalar_one()
    assert orphans == 0
