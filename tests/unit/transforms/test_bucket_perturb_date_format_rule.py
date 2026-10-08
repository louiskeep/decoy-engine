"""The one shared rule deciding whether a bucket_perturb `date_format` can write a date back.

The oracle parses with `date_format` and writes each perturbed date back with
`strftime(date_format)`. A format with no date directive writes its own literal
text over every parsed value, so the rule rejects it. Time, fractional-second
and timezone directives stay legal: the strategy perturbs the calendar date, so
they write midnight, zero or nothing, a documented property of the strategy.
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine.transforms.bucket_perturb import (
    bucket_perturb_date_format_problem,
    validate_bucket_perturb_config,
)

_REJECTED: list[Any] = [
    "ISO8601",
    "iso8601",
    "mixed",
    "MIXED",
    "YYYY-MM-DD",
    "foo",
    "%%Y",  # escaped percent then a literal Y
    "%",  # dangling
    "%H:%M:%S",  # time only
    "%f",
    "%z",
    "%Z",
    "%X",  # locale time only
    "%Q",  # unknown directive
    "%%%%Y",  # two escapes, no directive
    "%Y %Q",  # date plus unknown
    "%Y%",  # date plus dangling percent
    "%Y-%m-%d %",
    0,  # falsy non-string
    False,
    7,
    ["%Y"],
    {"fmt": "%Y"},
    b"%Y",
]

_ACCEPTED: list[str | None] = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%Y",
    "%x",
    "%c",
    "100%% %Y",
    "%%%Y",  # an escape, then %Y
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %f",
    "%Y-%m-%d %Z",
    "%Y-%m-%d %X",  # locale time beside a date
    "%%Q %Y",  # a literal %Q
    "%j-%Y",
    "%d-%b-%Y",
    "%m/%d/%y",
    "%A %d %B %G",
    "%U %w %a",
    None,  # autodetect, unchanged
    "",  # autodetect, unchanged
]


@pytest.mark.parametrize("fmt", _REJECTED, ids=[repr(f) for f in _REJECTED])
def test_unwritable_format_is_rejected_with_a_reason(fmt: Any) -> None:
    reason = bucket_perturb_date_format_problem(fmt)
    assert isinstance(reason, str) and reason


@pytest.mark.parametrize("fmt", _ACCEPTED, ids=[repr(f) for f in _ACCEPTED])
def test_writable_or_autodetect_format_is_accepted(fmt: str | None) -> None:
    assert bucket_perturb_date_format_problem(fmt) is None


def test_the_reason_asks_for_a_concrete_pattern() -> None:
    reason = bucket_perturb_date_format_problem("mixed")
    assert reason is not None
    assert "%Y-%m-%d" in reason


@pytest.mark.parametrize("fmt", ["mixed", "ISO8601", "%Q", 0, "%H:%M"])
def test_validator_applies_the_rule(fmt: Any) -> None:
    with pytest.raises(ValueError, match="date_format"):
        validate_bucket_perturb_config({"bucket": "month", "date_format": fmt})


@pytest.mark.parametrize("fmt", ["%Y-%m-%d", "", None])
def test_validator_accepts_a_writable_format_or_none(fmt: str | None) -> None:
    validate_bucket_perturb_config({"bucket": "month", "date_format": fmt})


def test_validator_accepts_an_absent_date_format() -> None:
    validate_bucket_perturb_config({"bucket": "month"})


def test_validator_still_checks_bucket_first() -> None:
    with pytest.raises(ValueError, match="bucket"):
        validate_bucket_perturb_config({"bucket": "nope", "date_format": "mixed"})
