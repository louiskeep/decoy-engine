"""A profile stamps `generated_at` in the same shape as every other writer: an aware UTC ISO text
ending `+00:00`. A document saved with the older `Z` suffix still loads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from decoy_engine.profiling.types import ColumnProfile


def _fresh() -> ColumnProfile:
    return ColumnProfile(source_label="t.csv", row_count=1, sample_strategy="full")


def test_fresh_generated_at_is_aware_utc_with_numeric_offset() -> None:
    stamp = _fresh().generated_at
    assert stamp.endswith("+00:00")
    assert not stamp.endswith("Z")
    parsed = datetime.fromisoformat(stamp)
    assert parsed.utcoffset() == timedelta(0)
    assert abs(datetime.now(timezone.utc) - parsed) < timedelta(minutes=5)


def test_a_stored_z_profile_still_loads() -> None:
    stored = {
        "source_label": "old.csv",
        "row_count": 3,
        "sample_strategy": "full",
        "generated_at": "2026-05-13T00:00:00.123456Z",
    }
    rebuilt = ColumnProfile(**stored)
    assert rebuilt.generated_at == "2026-05-13T00:00:00.123456Z"
    assert rebuilt.to_dict()["generated_at"] == "2026-05-13T00:00:00.123456Z"
