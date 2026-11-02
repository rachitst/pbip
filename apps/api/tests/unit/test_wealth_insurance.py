"""TASK-096 / ADR-0028: insurance eligibility hints are derived from entry ages only."""

import pytest
from pbip_api.modules.customers.demographics import AgeBand, band_for_age
from pbip_api.modules.wealth.insurance import BandHint, eligible_age_bands

pytestmark = pytest.mark.unit


def _hints(lo: int, hi: int) -> list[tuple[str, bool]]:
    return [(h.age_band.value, h.whole_band_eligible) for h in eligible_age_bands(lo, hi)]


def test_band_for_age_uses_the_profile_boundaries() -> None:
    assert [band_for_age(a) for a in (17, 18, 25, 26, 35, 36, 50, 51, 59, 60, 120)] == [
        AgeBand.UNKNOWN,
        AgeBand.AGE_18_25,
        AgeBand.AGE_18_25,
        AgeBand.AGE_26_35,
        AgeBand.AGE_26_35,
        AgeBand.AGE_36_50,
        AgeBand.AGE_36_50,
        AgeBand.AGE_51_59,
        AgeBand.AGE_51_59,
        AgeBand.AGE_60_PLUS,
        AgeBand.AGE_60_PLUS,
    ]


def test_entry_ages_18_to_60_cover_four_bands_whole_and_60_plus_only_in_part() -> None:
    assert _hints(18, 60) == [
        ("18-25", True),
        ("26-35", True),
        ("36-50", True),
        ("51-59", True),
        ("60+", False),  # 60 can apply, 61 cannot
    ]


def test_a_range_inside_one_band_is_partial_and_band_edges_are_exact() -> None:
    assert _hints(60, 75) == [("60+", False)]
    assert _hints(26, 35) == [("26-35", True)]
    assert _hints(25, 26) == [("18-25", False), ("26-35", False)]
    assert _hints(60, 120) == [("60+", True)]


def test_unknown_is_never_a_hint_and_an_inverted_range_is_refused() -> None:
    assert all(h.age_band is not AgeBand.UNKNOWN for h in eligible_age_bands(18, 120))
    assert eligible_age_bands(30, 30) == [BandHint(AgeBand.AGE_26_35, False)]
    with pytest.raises(ValueError, match="must not exceed"):
        eligible_age_bands(40, 30)
