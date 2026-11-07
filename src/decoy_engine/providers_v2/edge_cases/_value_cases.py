"""Numeric and temporal catalogues: integer, float, date and UTC datetime edge cases.

Boundaries follow two-value boundary-value analysis (ISO/IEC/IEEE 29119-4): a boundary and its
nearest neighbour across it, where the neighbour still fits the output type. Daylight-saving
instants are literals in past years (derived from the IANA tz database through ``zoneinfo``) so
the host's tz data can never change the catalogue; a test re-derives each one.
"""

from __future__ import annotations

import datetime as dt

from decoy_engine.providers_v2.edge_cases._spec import (
    DATETIME_MAX,
    DATETIME_MIN,
    EdgeCaseSpec,
    cases,
)

_UTC = dt.timezone.utc
_US = dt.timedelta(microseconds=1)


def _utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0, us: int = 0
) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, second, us, tzinfo=_UTC)


def _width_bounds(bits: int) -> tuple[int, ...]:
    low, high = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    return (low, high, low - 1, high + 1)


INTEGER_SPEC = EdgeCaseSpec(
    provider="edge_case_integer",
    kind="integer",
    categories=(
        "zero_and_unit",
        "signed_width_bounds",
        "unsigned_width_bounds",
        "float_precision",
        "decimal_digits",
    ),
    cases=(
        *cases("zero_and_unit", 0, 1, -1),
        *cases(
            "signed_width_bounds",
            *_width_bounds(8),
            *_width_bounds(16),
            *_width_bounds(32),
            # int64 has no outside neighbour that still fits.
            -(2**63),
            2**63 - 1,
        ),
        *cases("unsigned_width_bounds", 255, 256, 65535, 65536, 2**32 - 1, 2**32),
        *cases(
            "float_precision",
            2**24 + 1,  # first integer float32 cannot hold
            2**53 - 1,
            2**53,
            2**53 + 1,  # first integer float64 cannot hold
            -(2**53 - 1),
            -(2**53 + 1),
        ),
        *cases("decimal_digits", 9, 10, 99, 100, 999999999, 1000000000, 10**18 - 1, 10**18),
    ),
)

FLOAT_SPEC = EdgeCaseSpec(
    provider="edge_case_float",
    kind="float",
    categories=("zeros", "subnormal", "extremes", "precision", "rounding", "display"),
    cases=(
        *cases("zeros", 0.0, -0.0),
        *cases(
            "subnormal",
            5e-324,
            -5e-324,
            2.225073858507201e-308,  # largest subnormal
            2.2250738585072014e-308,  # smallest normal: the boundary
        ),
        *cases(
            "extremes",
            1.7976931348623157e308,
            -1.7976931348623157e308,
            3.4028234663852886e38,  # float32 max
            1.1754943508222875e-38,  # float32 smallest normal
            65504.0,  # float16 max
        ),
        *cases(
            "precision",
            2.220446049250313e-16,
            1.0000000000000002,
            0.1,
            0.1 + 0.2,
            9007199254740992.0,
            9007199254740994.0,
        ),
        *cases("rounding", 0.5, 1.5, 2.5, -2.5, 0.125, 1.005, 2.675),
        *cases("display", 1e-07, 1e16, 1e21, 123456789.12345679, 100.0),
    ),
)

D = dt.date

DATE_SPEC = EdgeCaseSpec(
    provider="edge_case_date",
    kind="date",
    categories=(
        "epoch",
        "leap_years",
        "period_ends",
        "rollovers",
        "calendar_history",
        "library_bounds",
        "type_bounds",
        "dst_days",
    ),
    cases=(
        *cases("epoch", D(1970, 1, 1), D(1969, 12, 31)),
        *cases(
            "leap_years",
            D(2000, 2, 29),
            D(2024, 2, 29),
            D(1900, 2, 28),
            D(1900, 3, 1),
            D(2100, 2, 28),
            D(2100, 3, 1),
        ),
        *cases("period_ends", D(2023, 2, 28), D(2023, 4, 30), D(2023, 12, 31), D(2024, 1, 1)),
        *cases(
            "rollovers",
            D(1999, 12, 31),
            D(2000, 1, 1),
            D(2038, 1, 19),
            D(2038, 1, 20),
            D(1899, 12, 30),  # spreadsheet serial day 0
            D(1899, 12, 31),
        ),
        *cases(
            "calendar_history",
            D(1582, 10, 4),
            D(1582, 10, 15),
            D(1752, 9, 2),
            D(1752, 9, 14),
            D(1753, 1, 1),  # SQL Server datetime minimum
        ),
        # A date column stays `object` in pandas and `date32` in Arrow across the whole range,
        # so dates outside the pandas nanosecond range are safe here (datetimes are not).
        *cases("library_bounds", D(1677, 9, 21), D(1677, 9, 22), D(2262, 4, 11), D(2262, 4, 12)),
        *cases("type_bounds", D(1, 1, 1), D(9999, 12, 31)),
        *cases("dst_days", D(2024, 3, 10), D(2024, 11, 3), D(2011, 12, 30)),
    ),
)

# (zone, instant the zone's UTC offset changes)
DST_TRANSITIONS: tuple[tuple[str, dt.datetime], ...] = (
    ("America/New_York", _utc(2024, 3, 10, 7)),
    ("America/New_York", _utc(2024, 11, 3, 6)),
    ("Europe/London", _utc(2024, 3, 31, 1)),
    ("Europe/London", _utc(2024, 10, 27, 1)),
    ("Australia/Lord_Howe", _utc(2024, 4, 6, 15)),  # 30-minute change
    ("Australia/Lord_Howe", _utc(2024, 10, 5, 15, 30)),
    ("Pacific/Apia", _utc(2011, 12, 30, 10)),  # 2011-12-30 skipped locally
    ("America/Sao_Paulo", _utc(2018, 11, 4, 3)),  # change at local midnight
)

DATETIME_SPEC = EdgeCaseSpec(
    provider="edge_case_datetime",
    kind="datetime",
    categories=(
        "epoch",
        "rollovers",
        "day_boundaries",
        "subsecond",
        "dst_transitions",
        "library_bounds",
    ),
    cases=(
        *cases("epoch", _utc(1970, 1, 1), _utc(1970, 1, 1) - _US),
        *cases(
            "rollovers",
            _utc(2038, 1, 19, 3, 14, 7),  # signed 32-bit seconds
            _utc(2038, 1, 19, 3, 14, 8),
            _utc(2106, 2, 7, 6, 28, 15),  # unsigned 32-bit seconds
            _utc(2106, 2, 7, 6, 28, 16),
        ),
        *cases(
            "day_boundaries",
            _utc(2024, 2, 29),
            _utc(2024, 2, 29, 23, 59, 59, 999999),
            _utc(2023, 12, 31, 23, 59, 59, 999999),
            # A leap second followed; ':60' is not representable, so stop one microsecond short.
            _utc(2016, 12, 31, 23, 59, 59, 999999),
        ),
        *cases(
            "subsecond",
            _utc(2024, 1, 1, 0, 0, 0, 1),
            _utc(2024, 1, 1, 0, 0, 0, 500000),
            _utc(2024, 1, 1, 0, 0, 0, 999999),
        ),
        *cases(
            "dst_transitions",
            *(v for _, t in DST_TRANSITIONS for v in (t - _US, t)),
        ),
        *cases("library_bounds", DATETIME_MIN, DATETIME_MAX),
    ),
)
