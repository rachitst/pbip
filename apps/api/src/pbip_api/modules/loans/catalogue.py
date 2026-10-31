"""Loan products offered by PBIP (TASK-045). A fixed catalogue in code: rates are indicative
annual rates in basis points; the customer sees them as "from X% p.a.", subject to eligibility."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class LoanType(StrEnum):
    PERSONAL = "PERSONAL"
    AUTO = "AUTO"
    HOME = "HOME"


@dataclass(frozen=True, slots=True)
class LoanProduct:
    id: UUID
    loan_type: LoanType
    name: str
    annual_rate_bps: int
    min_amount_minor: int
    max_amount_minor: int
    tenures_months: tuple[int, ...]


PRODUCTS: tuple[LoanProduct, ...] = (
    LoanProduct(
        UUID("10000000-0000-4000-a000-000000000001"),
        LoanType.PERSONAL,
        "Personal loan",
        1_150,
        25_000_00,
        15_00_000_00,
        (12, 24, 36, 48, 60),
    ),
    LoanProduct(
        UUID("10000000-0000-4000-a000-000000000002"),
        LoanType.AUTO,
        "Car loan",
        925,
        1_00_000_00,
        50_00_000_00,
        (12, 24, 36, 48, 60, 72, 84),
    ),
    LoanProduct(
        UUID("10000000-0000-4000-a000-000000000003"),
        LoanType.HOME,
        "Home loan",
        850,
        5_00_000_00,
        5_00_00_000_00,
        (60, 120, 180, 240, 300, 360),
    ),
)

_BY_ID = {product.id: product for product in PRODUCTS}


def get_product(product_id: UUID) -> LoanProduct | None:
    return _BY_ID.get(product_id)
