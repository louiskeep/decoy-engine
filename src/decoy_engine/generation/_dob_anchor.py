"""Seed-stable date-of-birth generation against a fixed anchor date.

Faker's ``date_of_birth`` computes its age window from ``datetime.now()``, so the same seed
yields different dates on different calendar days. ``anchored_date_of_birth`` is the same
algorithm with ``DOB_ANCHOR`` in place of "now": a pure function of (Faker seed, anchor, ages),
with no wall-clock read. It still draws through Faker's own ``date_time_ad`` and ``change_year``,
so the date distribution is Faker's, not a reimplementation.

Source of truth: ``faker.providers.date_time.Provider.date_of_birth`` (Faker 40.x). The
differential test in ``tests/unit/generation/test_dob_anchor.py`` compares this against upstream
with the clock frozen to the anchor, so a Faker change to that formula fails loudly.

``DOB_ANCHOR`` equals the date of the F0 oracle clock (``tests/parity/_f0_fixtures.ORACLE_CLOCK``),
so values are byte-identical to the frozen oracle recorded before this module existed.
"""

from __future__ import annotations

import datetime
from datetime import timedelta
from typing import Any

from faker import Faker
from faker.providers import date_time as _faker_date_time

from decoy_engine.errors import DecoyError

DOB_ANCHOR = datetime.date(2026, 10, 16)


def _check_faker_symbols() -> None:
    # Runtime raises, not `assert`: `python -O` strips asserts, and a silent fallback to the wall
    # clock is exactly the failure this module exists to prevent.
    if not callable(getattr(_faker_date_time, "change_year", None)):
        raise DecoyError(
            "faker.providers.date_time.change_year is missing; the dob anchor cannot be built "
            "against this Faker version."
        )
    if not callable(getattr(Faker(), "date_time_ad", None)):
        raise DecoyError(
            "Faker.date_time_ad is missing; the dob anchor cannot be built against this "
            "Faker version."
        )


_check_faker_symbols()

change_year = _faker_date_time.change_year


def anchored_date_of_birth(
    fake: Any,
    tzinfo: Any = None,
    minimum_age: int = 0,
    maximum_age: int = 115,
) -> datetime.date:
    """Return ``fake.date_of_birth`` computed relative to ``DOB_ANCHOR`` instead of today.

    Parameters are positional-or-keyword, matching Faker's signature: formulas call
    ``date_of_birth(None, 18, 65)`` positionally.
    """
    if not isinstance(minimum_age, int):
        raise TypeError("minimum_age must be an integer.")
    if not isinstance(maximum_age, int):
        raise TypeError("maximum_age must be an integer.")
    if maximum_age < 0:
        raise ValueError("maximum_age must be greater than or equal to zero.")
    if minimum_age < 0:
        raise ValueError("minimum_age must be greater than or equal to zero.")
    if minimum_age > maximum_age:
        raise ValueError("minimum_age must be less than or equal to maximum_age.")

    # Add one year to the age cap so the full range is reachable, then step off the boundary
    # (same rule as upstream).
    start = change_year(DOB_ANCHOR, -(maximum_age + 1))
    end = change_year(DOB_ANCHOR, -minimum_age)
    dob = fake.date_time_ad(tzinfo=tzinfo, start_datetime=start, end_datetime=end).date()
    return dob if dob != start else dob + timedelta(days=1)
