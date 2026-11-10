"""The fast full-frame profile gives the same answer as matching every cell.

`profile_columns` reads every row (head-only sampling leaked a late sensitive
value). The fast path matches each distinct value once and weights by count,
probes dates on a 200-row head before coercing the full series, and computes
k-anonymity from factorized codes. The golden file holds whole profiles produced
by the per-cell code that preceded it (origin/main at b63c0fb9), over the small
fixtures and a mixed 50k-row frame. Regenerate only on a trusted baseline with
`python -m tests.unit.profiling._write_full_frame_golden`.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from decoy_engine.profiling import profile_columns
from tests.unit.profiling._full_frame_frames import mixed_frame, small_frames

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "profile_full_frame" / "profiles.json"
_GOLDEN: dict[str, Any] = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}


def dump_profile(df: pd.DataFrame) -> dict[str, Any]:
    out = asdict(profile_columns(df, "t"))
    out.pop("generated_at")  # wall-clock stamp, differs between any two runs
    for f in out["fields"]:
        # Which value wins a count tie in value_counts depends on hash order, so it differs
        # between macOS and Linux and is not touched by this change. Keep the counts.
        f.pop("mode_value")
        for tv in f["top_values"]:
            tv.pop("value")
    return json.loads(json.dumps(out, sort_keys=True, default=str))


@pytest.mark.parametrize("key", list(small_frames()))
def test_small_frame_profile_matches_pre_change_golden(key: str) -> None:
    assert dump_profile(small_frames()[key]) == _GOLDEN[key]


def test_mixed_50k_profile_matches_pre_change_golden() -> None:
    got = dump_profile(mixed_frame())
    # Guard against a vacuous pass: several detectors must really have fired.
    fired = {dm["detector_id"] for f in got["fields"] for dm in f["detector_matches"] or []}
    assert {"email", "ssn", "pan", "ipv4", "us_phone", "iso_date"} <= fired
    assert got == _GOLDEN["mixed_50k"]


def test_late_sensitive_values_are_still_flagged() -> None:
    """Empty for the first 10,010 of 10,050 rows, then 40 emails: flagged, rate exact."""
    emails = [f"late{i}@example.com" for i in range(40)]
    df = pd.DataFrame({"contact": [None] * 10_010 + emails, "other": range(10_050)})
    field = next(f for f in profile_columns(df, "t").fields if f.name == "contact")
    assert [m.detector_id for m in field.detector_matches] == ["email"]
    assert field.detector_matches[0].match_rate == 1.0
    assert field.pii_score > 0


def test_late_rare_value_moves_the_rate() -> None:
    """Values past row 10,000 count toward the match rate, not just the head."""
    values = ["plain text"] * 10_000 + [f"x{i}@example.com" for i in range(10_000)]
    df = pd.DataFrame({"contact": values})
    field = profile_columns(df, "t").fields[0]
    email = next(m for m in field.detector_matches if m.detector_id == "email")
    assert email.match_rate == 0.5
    assert email.sample_misses == ["plain text"] * 3


def test_dates_after_a_non_date_head_skip_full_coercion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Free text never pays for a full-series to_datetime, and gets no date fields."""
    from decoy_engine.profiling import _date_sniff

    calls: list[int] = []
    real = _date_sniff._coerce

    def spy(values: Any) -> Any:
        calls.append(len(values))
        return real(values)

    monkeypatch.setattr(_date_sniff, "_coerce", spy)
    df = pd.DataFrame({"notes": [f"free text {i}" for i in range(5_000)]})
    field = profile_columns(df, "t").fields[0]
    assert calls == [200]
    assert field.date_min is None and field.invalid_count is None
