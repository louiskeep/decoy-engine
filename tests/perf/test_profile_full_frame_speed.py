"""Profiling every row stays fast: 1M rows x 8 columns in seconds, not minutes.

`decoy fit` profiles the whole frame so a sensitive column cannot slip past
detection. Before the unique-value detectors and the head-first date probe this
took minutes (it was 1.1 s on a head sample). The bound is generous (about 7 s
measured on an 8 vCPU VM, 298 s before) so only a return to per-cell regex or
full-series date coercion trips it. Linux only: macOS timings and allocator
behavior differ and the CI perf lane is Linux.
"""

from __future__ import annotations

import sys
import time

import numpy as np
import pandas as pd
import pytest

from decoy_engine.profiling import profile_columns

pytestmark = [
    pytest.mark.perf,
    pytest.mark.serial,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only"),
]

ROWS = 1_000_000
BOUND_SECONDS = 20.0


def _frame() -> pd.DataFrame:
    rng = np.random.default_rng(11)
    pool = rng.integers(0, 50_000, ROWS)
    return pd.DataFrame(
        {
            "email": pd.Series(pool).map(lambda i: f"user{i}@example.com"),
            "ssn": pd.Series(pool % 9000).map(lambda i: f"{100 + i % 800:03d}-45-{1000 + i:04d}"),
            "phone": pd.Series(pool).map(lambda i: f"(555) 555-{i % 10000:04d}"),
            "created": pd.Series(pool % 3000).map(lambda i: f"20{10 + i % 14}-{1 + i % 12:02d}-15"),
            "notes": pd.Series(pool % 20_000).map(lambda i: f"visit note {i} follow up"),
            "city": rng.choice(["Austin", "Boston", "Chicago", "Denver"], ROWS),
            "account_id": pd.Series(pool).map(lambda i: f"AC{i:07d}"),
            "amount": rng.integers(0, 100_000, ROWS),
        }
    )


def test_one_million_rows_eight_columns_profiles_in_seconds() -> None:
    df = _frame()
    start = time.perf_counter()
    profile = profile_columns(df, "perf")
    elapsed = time.perf_counter() - start
    assert profile.row_count == ROWS
    fired = {f.name: [m.detector_id for m in f.detector_matches] for f in profile.fields}
    assert "email" in fired["email"] and "ssn" in fired["ssn"]
    assert elapsed < BOUND_SECONDS, f"profiled 1M x 8 in {elapsed:.1f}s (bound {BOUND_SECONDS}s)"
