import csv
from pathlib import Path

import pytest
from pbip_datagen.__main__ import main
from pbip_datagen.loader import to_conninfo

pytestmark = pytest.mark.unit


def test_export_writes_reproducible_csv(tmp_path: Path) -> None:
    args = ["export", "--seed", "11", "--customers", "4", "--months", "2"]
    assert main([*args, "--out", str(tmp_path / "a")]) == 0
    assert main([*args, "--out", str(tmp_path / "b")]) == 0
    for name in ("customers.csv", "transactions.csv"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    with (tmp_path / "a" / "customers.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4 and {"persona", "monthly_income_minor", "churned_on"} <= set(rows[0])


def test_invalid_config_exits_with_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["export", "--customers", "0", "--out", "unused"]) == 2
    assert "customers must be" in capsys.readouterr().err


def test_load_requires_a_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["load", "--customers", "1", "--months", "1"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql+psycopg://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgresql://u:p@h/db", "postgresql://u:p@h/db"),
    ],
)
def test_sqlalchemy_urls_are_converted_for_libpq(url: str, expected: str) -> None:
    assert to_conninfo(url) == expected
