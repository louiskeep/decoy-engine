"""`parse_megabytes` is the one reader of the "NNMB" memory-limit string."""

from __future__ import annotations

import pytest

from decoy_engine._memory_limit import parse_megabytes


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        ("512MB", 512),
        ("0MB", 0),
        ("1MB", 1),
        ("512", None),
        ("512mb", None),
        ("MB", None),
        ("-1MB", None),
        ("5 MB", None),
        ("5MB\n", None),
        ("1GB", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_megabytes(limit: str | None, expected: int | None) -> None:
    assert parse_megabytes(limit) == expected
