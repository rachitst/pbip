"""The loader COPYs transactions in index-key order without changing the data (#135).

Key order appends at the right edge of ix_transactions_customer_id_occurred_at instead of splitting
pages across it; the 1,000-customer COPY budget is checked by the integration test
test_copy_loads_1000_customers_in_under_15_seconds.
"""

from collections import Counter
from typing import Any

import pytest
from pbip_datagen import loader
from pbip_datagen.generator import GeneratorConfig, generate

pytestmark = pytest.mark.unit


def test_in_index_order_sorts_by_the_index_key_and_keeps_every_row() -> None:
    dataset = generate(GeneratorConfig(seed=7, customers=12, months=2))
    ordered = loader.in_index_order(dataset.transactions)
    keys = [(t.customer_id, t.occurred_at) for t in ordered]
    assert keys == sorted(keys)
    assert Counter(ordered) == Counter(dataset.transactions)


def test_load_dataset_copies_transactions_in_index_order(monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = generate(GeneratorConfig(seed=7, customers=12, months=2))
    copied: dict[str, list[tuple[Any, ...]]] = {}

    class _Cursor:
        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, *_args: object) -> None:
            return None

    class _Connection(_Cursor):
        def cursor(self) -> _Cursor:
            return _Cursor()

        def commit(self) -> None:
            return None

    def fake_copy(_cur: object, table: str, _cols: object, _types: object, rows: Any) -> None:
        copied[table] = list(rows)

    monkeypatch.setattr(loader.psycopg, "connect", lambda _url: _Connection())
    monkeypatch.setattr(loader, "_copy", fake_copy)
    report = loader.load_dataset(dataset, "postgresql://unused")

    keys = [(row[2], row[3]) for row in copied["transactions"]]  # customer_id, occurred_at
    assert keys == sorted(keys)
    assert len(copied["transactions"]) == report.transactions == len(dataset.transactions)
    assert {row[0] for row in copied["transactions"]} == {t.id for t in dataset.transactions}
