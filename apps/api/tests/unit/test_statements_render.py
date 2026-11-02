"""TASK-016: the dependency-free PDF writer and the CSV/period rules, without a database."""

import re
from datetime import UTC, date, datetime

import pytest
from pbip_api.core.errors import UnprocessableError
from pbip_api.modules.statements.pdf import PdfDocument
from pbip_api.modules.statements.service import (
    BANK_TZ,
    Line,
    Statement,
    period_bounds,
    render_csv,
    render_pdf,
)

pytestmark = pytest.mark.unit


def _xref_is_consistent(pdf: bytes) -> bool:
    """Every xref offset must point at the start of its 'n 0 obj' line."""
    found = re.search(rb"startxref\n(\d+)", pdf)
    assert found is not None
    start = int(found.group(1))
    assert pdf[start:].startswith(b"xref")
    offsets = [int(m) for m in re.findall(rb"(\d{10}) 00000 n ", pdf)]
    return all(pdf[o:].startswith(b"%d 0 obj" % (i + 1)) for i, o in enumerate(offsets))


def test_pdf_writer_produces_a_valid_cross_reference_and_escapes_text() -> None:
    doc = PdfDocument("t")
    doc.text(10, 10, "a (b) c\\d")
    doc.add_page()
    doc.text(10, 10, "page two ₹ •")
    out = doc.render()
    assert out.startswith(b"%PDF-1.4") and out.endswith(b"%%EOF\n")
    assert b"(a \\(b\\) c\\\\d)" in out
    # Bullet maps to WinAnsi 0x95; the rupee sign is outside WinAnsi and becomes '?'.
    assert b"(page two ? \x95)" in out
    assert b"/Count 2" in out
    assert _xref_is_consistent(out)


def _statement(lines: list[Line]) -> Statement:
    now = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
    return Statement(
        holder="Asha Rao",
        account_type="SAVINGS",
        account_masked="••••1234",
        currency="INR",
        start=date(2026, 10, 1),
        end=date(2026, 10, 31),
        opening=1_000,
        closing=1_000 + sum(x.credit - x.debit for x in lines),
        total_debits=sum(x.debit for x in lines),
        total_credits=sum(x.credit for x in lines),
        lines=lines,
        generated_at=now.astimezone(BANK_TZ),
    )


def _line(i: int) -> Line:
    return Line(
        posted_at=datetime(2026, 10, 5, tzinfo=BANK_TZ),
        description="Transfer",
        reference=f"ref {i}",
        debit=5,
        credit=0,
        balance_after=1_000 - 5 * (i + 1),
    )


def test_long_statements_flow_onto_further_pages_with_a_footer_each() -> None:
    out = render_pdf(_statement([_line(i) for i in range(120)]))
    assert out.count(b"/Type /Page ") == 3
    assert b"Page 3" in out and _xref_is_consistent(out)


def test_empty_period_says_so() -> None:
    assert b"No transactions in this period." in render_pdf(_statement([]))


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_csv_neutralises_formula_injection_and_uses_plain_decimals(prefix: str) -> None:
    line = Line(datetime(2026, 10, 5, tzinfo=BANK_TZ), "Transfer", f"{prefix}cmd", 12_345, 0, -5)
    rows = render_csv(_statement([line])).decode().splitlines()
    assert rows[0] == "date,description,reference,debit,credit,balance,currency"
    assert rows[1] == f"2026-10-05T00:00:00+05:30,Transfer,'{prefix}cmd,123.45,,-0.05,INR"


def test_period_is_ist_days_inclusive_and_bounded() -> None:
    begin, finish = period_bounds(date(2026, 10, 1), date(2026, 10, 31), date(2026, 11, 2))
    assert begin == datetime(2026, 9, 30, 18, 30, tzinfo=UTC)
    assert finish == datetime(2026, 10, 31, 18, 30, tzinfo=UTC)
    for start, end in (
        (date(2026, 10, 2), date(2026, 10, 1)),
        (date(2026, 11, 1), date(2026, 11, 3)),
        (date(2025, 1, 1), date(2026, 1, 2)),
    ):
        with pytest.raises(UnprocessableError):
            period_bounds(start, end, date(2026, 11, 2))
