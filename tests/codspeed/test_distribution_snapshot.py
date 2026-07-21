"""CodSpeed: distribution-snapshot compute, the measurement primitive that
D1b (diagnostic) and D1c (fidelity) both build on
(``decoy_engine.quality.snapshot.compute_distribution_snapshot``).

Every masking/generation job that runs a quality check calls this once per
side (source, output) per table. It walks every column (numeric binning,
categorical top-K, datetime, freetext) plus any requested joint pairs, so
its cost scales with columns x rows -- a representative "shape work" hot
path distinct from the per-cell transform benchmarked in
test_fpe_transform.py.

5,000 rows x 6 columns (numeric, categorical, datetime, freetext, plus one
joint pair) mirrors the column-kind mix a real HIPAA-shaped table exercises
(see tests/benchmark/test_arrow_to_pandas_conversion.py's fixture) while
keeping a `--codspeed` run fast.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from decoy_engine.quality.snapshot import compute_distribution_snapshot

pytestmark = pytest.mark.codspeed

_ROW_COUNT = 5_000


def _mixed_frame(rows: int) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    depts = ["eng", "sales", "ops", "hr", "finance"]
    return pd.DataFrame(
        {
            "amount": rng.normal(loc=500.0, scale=120.0, size=rows),
            "score": rng.uniform(low=0.0, high=1.0, size=rows),
            "dept": [depts[i % len(depts)] for i in range(rows)],
            "signup_date": pd.date_range("2020-01-01", periods=rows, freq="h"),
            "notes": [f"customer note number {i} regarding account activity" for i in range(rows)],
            "region": [depts[(i * 3) % len(depts)] for i in range(rows)],
        }
    )


_FRAME = _mixed_frame(_ROW_COUNT)


def test_compute_distribution_snapshot(benchmark) -> None:
    def _run() -> dict:
        return compute_distribution_snapshot(
            _FRAME,
            joint_columns=[("dept", "region")],
        )

    snapshot = benchmark(_run)

    assert snapshot["row_count"] == _ROW_COUNT
    assert set(snapshot["columns"]) == set(_FRAME.columns)
    assert len(snapshot["joints"]) == 1
