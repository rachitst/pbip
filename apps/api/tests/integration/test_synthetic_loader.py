"""TASK-009: COPY bulk load of the synthetic dataset and the schema's DB-level integrity rules."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pbip_datagen.generator import GeneratorConfig, generate
from pbip_datagen.loader import load_dataset
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


def test_copy_loads_1000_customers_in_under_15_seconds(
    synthetic: Engine, test_database_url: str
) -> None:
    dataset = generate(GeneratorConfig(seed=42, customers=1000, months=12))
    report = load_dataset(dataset, test_database_url)

    assert report.seconds < 15, f"COPY load took {report.seconds:.1f}s"
    assert (report.customers, report.transactions) == (1000, len(dataset.transactions))
    assert _scalar(synthetic, "SELECT count(*) FROM synthetic.customers") == 1000
    assert _scalar(synthetic, "SELECT count(*) FROM synthetic.transactions") == report.transactions
    loaded_total = _scalar(synthetic, "SELECT sum(amount_minor) FROM synthetic.transactions")
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
