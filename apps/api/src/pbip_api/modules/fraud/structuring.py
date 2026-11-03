"""Deposit-side structuring detection (F7, #274, ADR-0051): the pure core, no database.

Structuring is splitting money into several credits that each stay just under a reporting
threshold. The per-transfer STRUCTURING rule (ADR-0046) sees it only when the customer next sends
money; a structurer who never pays out is never scored. The batch monitor
(``structuring_monitor``) therefore scans received credits, and this module decides which of them
form a pattern. ``scripts/structuring_monitor_eval.py`` runs the same function over a synthetic
export, so the measured detection and false-positive rates are those of the served logic.

Definitions (the same settings as the STRUCTURING rule, ``FRAUD_STRUCTURING_*``):

* **in band**: ``amount >= (100 - band)% of threshold`` and ``amount < threshold``, compared in
  integers (``amount * 100 >= threshold * (100 - band)``), so the boundary is exact.
* **pattern**: at least ``min_count`` in-band credits on ONE account whose first and last are at
  most ``window`` apart (inclusive, as the rule's ``created_at >= now - window``).
* **covered**: a credit that already belongs to an earlier alert. Covered credits are left out
  before searching, so the same credit never opens a second alert, however the windows overlap.

The search is leftmost-greedy and deterministic: take the earliest uncovered in-band credit, extend
the pattern to every credit within ``window`` of it, and if there are ``min_count`` or more, that
is one pattern and the search continues after its last credit; otherwise it moves one credit on.
"""

from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby

# The monitor scans two windows back on every hourly run: every pattern is then seen whole in at
# least one run, however its window falls (ADR-0051).
LOOKBACK_WINDOWS = 2


@dataclass(frozen=True, slots=True)
class StructuringBand:
    threshold_minor: int
    band_percent: int
    window: timedelta
    min_count: int

    def __post_init__(self) -> None:
        if self.threshold_minor < 1 or not 1 <= self.band_percent <= 99:
            raise ValueError("threshold must be positive and the band within 1-99 percent")
        if self.window <= timedelta(0) or self.min_count < 2:
            raise ValueError("window must be positive and min_count at least 2")

    def in_band(self, amount_minor: int) -> bool:
        floor_x100 = self.threshold_minor * (100 - self.band_percent)
        return amount_minor * 100 >= floor_x100 and amount_minor < self.threshold_minor


@dataclass(frozen=True, slots=True)
class Credit:
    """One received credit: ids are opaque (UUIDs in the API, strings in the offline script)."""

    transaction_id: Hashable
    account_id: Hashable
    created_at: datetime
    amount_minor: int


@dataclass(frozen=True, slots=True)
class Pattern:
    account_id: Hashable
    credits: tuple[Credit, ...]

    @property
    def total_minor(self) -> int:
        return sum(c.amount_minor for c in self.credits)

    @property
    def last(self) -> Credit:
        return self.credits[-1]


def find_patterns(
    credits: Iterable[Credit], band: StructuringBand, covered: frozenset[Hashable] = frozenset()
) -> list[Pattern]:
    """Every structuring pattern among ``credits`` (any accounts, any order), oldest first."""
    eligible = sorted(
        (c for c in credits if band.in_band(c.amount_minor) and c.transaction_id not in covered),
        key=lambda c: (str(c.account_id), c.created_at, str(c.transaction_id)),
    )
    patterns: list[Pattern] = []
    for account_id, group in groupby(eligible, key=lambda c: c.account_id):
        patterns.extend(_account_patterns(account_id, list(group), band))
    return sorted(patterns, key=lambda p: (p.credits[0].created_at, str(p.account_id)))


def _account_patterns(
    account_id: Hashable, credits: list[Credit], band: StructuringBand
) -> list[Pattern]:
    found, start = [], 0
    while start < len(credits):
        end = start
        while (
            end + 1 < len(credits)
            and credits[end + 1].created_at - credits[start].created_at <= band.window
        ):
            end += 1
        if end - start + 1 >= band.min_count:
            found.append(Pattern(account_id, tuple(credits[start : end + 1])))
            start = end + 1
        else:
            start += 1
    return found
