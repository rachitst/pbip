"""Insurance eligibility hints (TASK-096, ADR-0028): pure functions, no customer data.

A hint says which ADR-0015 age bands overlap a product's entry-age range, and whether every age
in the band can apply or only some of them. It is NOT underwriting: it never looks at a customer,
and an insurer would still decide eligibility and the premium. The band boundaries come from
``customers.demographics.band_for_age`` so they can never drift from the profile's bands.
"""

from dataclasses import dataclass

from pbip_api.modules.customers.demographics import MAX_AGE, MIN_AGE, AgeBand, band_for_age


@dataclass(frozen=True, slots=True)
class BandHint:
    age_band: AgeBand
    whole_band_eligible: bool  # False: only part of the band is inside the entry ages


def _band_ages() -> dict[AgeBand, range]:
    """Each band's ages within the ages the bank accepts (18 to 120), in band order."""
    bands: dict[AgeBand, list[int]] = {}
    for age in range(MIN_AGE, MAX_AGE + 1):
        bands.setdefault(band_for_age(age), []).append(age)
    return {band: range(ages[0], ages[-1] + 1) for band, ages in bands.items()}


_BAND_AGES = _band_ages()


def eligible_age_bands(min_entry_age: int, max_entry_age: int) -> list[BandHint]:
    """The bands overlapping ``[min_entry_age, max_entry_age]``, youngest first."""
    if min_entry_age > max_entry_age:
        raise ValueError("min_entry_age must not exceed max_entry_age")
    hints: list[BandHint] = []
    for band, ages in _BAND_AGES.items():
        if ages.stop - 1 < min_entry_age or ages.start > max_entry_age:
            continue
        whole = min_entry_age <= ages.start and ages.stop - 1 <= max_entry_age
        hints.append(BandHint(age_band=band, whole_band_eligible=whole))
    return hints
