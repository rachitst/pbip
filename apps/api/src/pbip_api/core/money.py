"""Money value object. Amounts are INTEGER MINOR UNITS (paise/pence). Floats are forbidden."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

DEFAULT_CURRENCY = "INR"
_EXPONENT = 2
_SCALE = 10**_EXPONENT
_CURRENCY_LEN = 3


class MoneyError(ValueError):
    """Raised for invalid amounts or mixed-currency arithmetic."""


@dataclass(frozen=True, slots=True)
class Money:
    minor_units: int
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        if isinstance(self.minor_units, bool) or not isinstance(self.minor_units, int):
            raise MoneyError("minor_units must be an int")
        if len(self.currency) != _CURRENCY_LEN or not self.currency.isupper():
            raise MoneyError("currency must be a 3-letter upper-case ISO code")

    @classmethod
    def from_decimal(cls, amount: Decimal | str | int, currency: str = DEFAULT_CURRENCY) -> "Money":
        if isinstance(amount, float):
            raise MoneyError("floats are not accepted for money")
        try:
            value = Decimal(amount)
        except InvalidOperation as exc:
            raise MoneyError("invalid amount") from exc
        if not value.is_finite():
            raise MoneyError("amount must be finite")
        scaled = value * _SCALE
        if scaled != scaled.to_integral_value():
            raise MoneyError(f"amount has more than {_EXPONENT} decimal places")
        return cls(int(scaled), currency)

    def to_decimal(self) -> Decimal:
        return Decimal(self.minor_units) / Decimal(_SCALE)

    def _check(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise MoneyError("currency mismatch")

    def __add__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.minor_units + other.minor_units, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.minor_units - other.minor_units, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.minor_units, self.currency)

    def __lt__(self, other: "Money") -> bool:
        self._check(other)
        return self.minor_units < other.minor_units

    def __le__(self, other: "Money") -> bool:
        self._check(other)
        return self.minor_units <= other.minor_units

    def __gt__(self, other: "Money") -> bool:
        self._check(other)
        return self.minor_units > other.minor_units

    def __ge__(self, other: "Money") -> bool:
        self._check(other)
        return self.minor_units >= other.minor_units

    def is_positive(self) -> bool:
        return self.minor_units > 0


def group_indian(whole: int) -> str:
    """Indian digit grouping (lakh/crore): 12345678 -> "1,23,45,678". Non-negative input."""
    digits = str(whole)
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    pairs = [head[max(i - 2, 0) : i] for i in range(len(head), 0, -2)][::-1]
    return ",".join([*pairs, tail])


def format_minor(minor_units: int, currency: str = DEFAULT_CURRENCY) -> str:
    """Human display, e.g. ``INR 1,00,000.50``. For messages only - never parse this back.

    Indian grouping, exactly like the web's ``formatMinor`` (``lib/money.ts``), so an amount reads
    the same in the assistant's reply, a notification and the Accounts page.
    """
    sign = "-" if minor_units < 0 else ""
    whole, paise = divmod(abs(minor_units), _SCALE)
    return f"{sign}{currency} {group_indian(whole)}.{paise:02d}"
