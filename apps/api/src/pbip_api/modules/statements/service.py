"""Account statements (TASK-016, UC-03): one period of ledger entries as CSV or PDF.

Read-only over the ledger (``ledger.service.entries_in_period`` / ``balance_before``). The only
writes are evidence: an audit row and a "statement downloaded" notification, so a customer is told
when their financial history leaves the bank.
"""

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from pbip_api.core.calendar import BANK_TZ
from pbip_api.core.errors import UnprocessableError
from pbip_api.core.money import format_minor
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.accounts.service import mask_account_number
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import EntryDirection
from pbip_api.modules.ledger.service import PeriodEntry
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.statements.pdf import A4_HEIGHT, PdfDocument

MAX_PERIOD_DAYS = 366
MAX_ENTRIES = 10_000


class StatementFormat(StrEnum):
    CSV = "csv"
    PDF = "pdf"


@dataclass(frozen=True)
class Line:
    posted_at: datetime
    description: str
    reference: str
    debit: int
    credit: int
    balance_after: int


@dataclass(frozen=True)
class Statement:
    holder: str
    account_type: str
    account_masked: str
    currency: str
    start: date
    end: date
    opening: int
    closing: int
    total_debits: int
    total_credits: int
    lines: list[Line]
    generated_at: datetime


@dataclass(frozen=True)
class StatementFile:
    content: bytes
    media_type: str
    filename: str


def _describe(entry: PeriodEntry) -> str:
    words = entry.type.value.replace("_", " ").capitalize()
    return words if entry.status.value == "POSTED" else f"{words} ({entry.status.value.lower()})"


def _line(entry: PeriodEntry) -> Line:
    debit = entry.amount if entry.direction == EntryDirection.DEBIT else 0
    return Line(
        posted_at=entry.created_at.astimezone(BANK_TZ),
        description=_describe(entry),
        reference=entry.reference or "",
        debit=debit,
        credit=entry.amount - debit,
        balance_after=entry.balance_after,
    )


def period_bounds(start: date, end: date, today: date) -> tuple[datetime, datetime]:
    """Validate a statement period and return its [start, end) instants in UTC."""
    if start > end:
        raise UnprocessableError("the start date must be on or before the end date")
    if end > today:
        raise UnprocessableError("the statement cannot end in the future")
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise UnprocessableError(f"a statement can cover at most {MAX_PERIOD_DAYS} days")
    begin = datetime.combine(start, time.min, BANK_TZ).astimezone(UTC)
    finish = datetime.combine(end + timedelta(days=1), time.min, BANK_TZ).astimezone(UTC)
    return begin, finish


def build_statement(
    session: Session, account: Account, holder: str, start: date, end: date, now: datetime
) -> Statement:
    begin, finish = period_bounds(start, end, now.astimezone(BANK_TZ).date())
    rows = ledger.entries_in_period(session, account.id, begin, finish, MAX_ENTRIES + 1)
    if len(rows) > MAX_ENTRIES:
        raise UnprocessableError("too many transactions in this period; choose a shorter one")
    lines = [_line(entry) for entry in rows]
    if lines:
        first = lines[0]
        opening = first.balance_after - first.credit + first.debit
    else:
        opening = ledger.balance_before(session, account.id, begin)
    return Statement(
        holder=holder,
        account_type=account.account_type.value,
        account_masked=mask_account_number(account.account_number),
        currency=account.currency,
        start=start,
        end=end,
        opening=opening,
        closing=lines[-1].balance_after if lines else opening,
        total_debits=sum(line.debit for line in lines),
        total_credits=sum(line.credit for line in lines),
        lines=lines,
        generated_at=now.astimezone(BANK_TZ),
    )


def _plain(minor: int) -> str:
    """Minor units as a plain decimal string for CSV (no grouping, never a float)."""
    sign = "-" if minor < 0 else ""
    whole, paise = divmod(abs(minor), 100)
    return f"{sign}{whole}.{paise:02d}"


def _csv_safe(value: str) -> str:
    """Neutralise spreadsheet formula injection in free text (references)."""
    return f"'{value}" if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def render_csv(statement: Statement) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["date", "description", "reference", "debit", "credit", "balance", "currency"])
    for line in statement.lines:
        writer.writerow(
            [
                line.posted_at.isoformat(timespec="seconds"),
                line.description,
                _csv_safe(line.reference),
                _plain(line.debit) if line.debit else "",
                _plain(line.credit) if line.credit else "",
                _plain(line.balance_after),
                statement.currency,
            ]
        )
    return buffer.getvalue().encode("utf-8")


_LEFT, _RIGHT, _TOP, _BOTTOM = 40.0, 555.0, A4_HEIGHT - 50, 60.0
_ROW = 13.0


def _header(pdf: PdfDocument, s: Statement) -> float:
    def money(minor: int) -> str:
        return format_minor(minor, s.currency)

    pdf.text(_LEFT, _TOP, "PBIP Bank", 18, "bold")
    pdf.text(_LEFT, _TOP - 16, "Account statement", 11)
    y = _TOP - 44
    for label, value in (
        ("Account holder", s.holder),
        ("Account", f"{s.account_type.capitalize()} {s.account_masked}"),
        ("Period", f"{s.start:%d %b %Y} to {s.end:%d %b %Y} (IST)"),
        ("Opening balance", money(s.opening)),
        ("Money in", money(s.total_credits)),
        ("Money out", money(s.total_debits)),
        ("Closing balance", money(s.closing)),
    ):
        pdf.text(_LEFT, y, label, 10, "bold")
        pdf.text(_LEFT + 120, y, value, 10)
        y -= 14
    return y - 10


def _table_head(pdf: PdfDocument, y: float) -> float:
    pdf.text(_LEFT, y, "Date", 9, "bold")
    pdf.text(_LEFT + 70, y, "Description", 9, "bold")
    pdf.text(_LEFT + 210, y, "Reference", 9, "bold")
    for right, label in ((385, "Debit"), (465, "Credit"), (_RIGHT, "Balance")):
        pdf.text(right - len(label) * 5.0, y, label, 9, "bold")
    pdf.rule(_LEFT, y - 4, _RIGHT, y - 4)
    return y - _ROW - 4


def _footer(pdf: PdfDocument, s: Statement, page: int) -> None:
    pdf.text(
        _LEFT,
        30,
        f"Generated {s.generated_at:%d %b %Y %H:%M} IST. Simulated bank for a university project. "
        f"Page {page}",
        7,
    )


def render_pdf(statement: Statement) -> bytes:
    s = statement
    pdf = PdfDocument(f"PBIP statement {s.account_masked} {s.start} to {s.end}")
    pdf.add_page()
    page = 1
    y = _table_head(pdf, _header(pdf, s))
    if not s.lines:
        pdf.text(_LEFT, y, "No transactions in this period.", 9)
    for line in s.lines:
        if y < _BOTTOM:
            _footer(pdf, s, page)
            pdf.add_page()
            page += 1
            y = _table_head(pdf, _TOP)
        pdf.text(_LEFT, y, f"{line.posted_at:%d %b %Y}", 9, "mono")
        pdf.text(_LEFT + 70, y, line.description[:24], 9)
        pdf.text(_LEFT + 210, y, line.reference[:20], 9)
        if line.debit:
            pdf.text_right(385, y, _plain(line.debit))
        if line.credit:
            pdf.text_right(465, y, _plain(line.credit))
        pdf.text_right(_RIGHT, y, _plain(line.balance_after))
        y -= _ROW
    _footer(pdf, s, page)
    return pdf.render()


def download(
    session: Session,
    customer: Customer,
    account: Account,
    start: date,
    end: date,
    fmt: StatementFormat,
    now: datetime,
    meta: RequestMeta,
) -> StatementFile:
    """Build the file, then record the export (audit + notification) in one transaction."""
    statement = build_statement(session, account, customer.full_name, start, end, now)
    content = render_pdf(statement) if fmt == StatementFormat.PDF else render_csv(statement)
    _record(session, customer.user_id, account.id, statement, fmt, meta)
    session.commit()
    last4 = account.account_number[-4:]
    return StatementFile(
        content=content,
        media_type="application/pdf" if fmt == StatementFormat.PDF else "text/csv; charset=utf-8",
        filename=f"pbip-statement-{last4}-{start.isoformat()}-to-{end.isoformat()}.{fmt.value}",
    )


def _record(
    session: Session,
    user_id: UUID,
    account_id: UUID,
    statement: Statement,
    fmt: StatementFormat,
    meta: RequestMeta,
) -> None:
    period = {"start": statement.start.isoformat(), "end": statement.end.isoformat()}
    AuditService(session).record(
        user_id,
        "STATEMENT_DOWNLOADED",
        "account",
        str(account_id),
        ip=meta.ip,
        request_id=meta.request_id,
        details={**period, "format": fmt.value, "entries": len(statement.lines)},
    )
    notifications.enqueue_statement(
        session, user_id, account_id, statement.account_masked, period, fmt.value
    )
