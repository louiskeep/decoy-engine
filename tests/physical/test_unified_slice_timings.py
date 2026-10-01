"""A6 acceptance tests: unified-lane per-column timings end to end, via
`run_pipeline` (not the coordinator directly -- `tests/physical/
test_shadow_coordinator.py` covers the coordinator-level AT2/AT6 cases).

Plan: `docs/plans/2026-09-30-unified-lane-timings.md`.
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution import _unified_slice, _unified_slice_admission, run_pipeline
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.keyprovider import SecretKeyProvider
from tests.physical._shadow_helpers import build_config, write_read_only_fixture
from tests.physical.test_unified_slice_input_formats import (
    _NEEDS_COMPANION,
    _assert_full_parity,
    _csv_config,
    _run_both,
    _write_csv,
)

_MASK_KEY = bytes(range(32))


def _key_provider() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


# ---------------------------------------------------------------------------
# AT2: a Counter[(strategy_type, column)] of the lane's timings has exactly
# one record per admitted physical node, and its key set equals the pandas
# route's, across the native operators this slice admits.
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
def test_unified_lane_timings_cover_every_native_operator_matching_pandas_keys(
    tmp_path: Path,
) -> None:
    columns_data = {
        "id": ["1001", "1002", "1003", "1004"],
        "note": ["s1", "s2", "s3", "s4"],
        "code": ["12345", "6789", "1111", "2222"],
        "cat": ["red", "green", "blue", "red"],
        "event_date": ["2024-03-15", "2024-06-01", "2024-11-20", "2024-01-05"],
        "signup_date": ["2023-05-10", "2023-08-22", "2023-12-01", "2023-02-14"],
        "gb": ["g1", "g2", "g1", "g2"],
        "gk": ["seed", "seed", "seed", "seed"],
    }
    csv_path = tmp_path / "wide.csv"
    _write_csv(csv_path, columns_data)
    resident = pa.table({k: pa.array(v, type=pa.string()) for k, v in columns_data.items()})
    columns = [
        {"name": "id", "strategy": "hash", "namespace": "ns_id"},
        {"name": "note", "strategy": "redact"},
        {"name": "code", "strategy": "truncate", "provider_config": {"length": 3}},
        {
            "name": "cat",
            "strategy": "categorical",
            "namespace": "ns_cat",
            "deterministic": True,
            "provider_config": {"categories": ["red", "green", "blue"]},
        },
        {
            "name": "event_date",
            "strategy": "bucket_perturb",
            "namespace": "ns_bp",
            "provider_config": {"bucket": "month", "date_format": "%Y-%m-%d"},
        },
        {
            "name": "signup_date",
            "strategy": "date_shift",
            "namespace": "ns_ds",
            "provider_config": {"date_format": "%Y-%m-%d"},
        },
        {"name": "gb", "strategy": "passthrough"},
        {
            "name": "gk",
            "strategy": "group_key",
            "provider_config": {"group_by": "gb", "length": 16},
        },
    ]
    config = _csv_config(tmp_path, "t", csv_path, columns)
    off, on = _run_both(config, {"t": resident})
    _assert_full_parity(off, on)

    expected_keys = {
        ("hash", "id"),
        ("redact", "note"),
        ("truncate", "code"),
        ("categorical", "cat"),
        ("bucket_perturb", "event_date"),
        ("date_shift", "signup_date"),
        ("passthrough", "gb"),
        ("group_key", "gk"),
    }
    on_counts = Counter((t.strategy_type, t.column) for t in on.timings)
    off_counts = Counter((t.strategy_type, t.column) for t in off.timings)
    assert set(on_counts) == expected_keys, f"got {set(on_counts)}"
    assert set(on_counts) == set(off_counts), "unified-lane keys must match the pandas route's"
    assert all(count == 1 for count in on_counts.values()), "one record per admitted node"
    assert all(t.elapsed_ms >= 0 for t in on.timings)
    assert all(t.peak_memory_delta_kb >= 0 for t in on.timings)


# ---------------------------------------------------------------------------
# AT3: elapsed_ms >= 0, peak_memory_delta_kb >= 0 for every record; for a
# 150k-row admitted job (auto_chunk=False), sum(elapsed_ms) > 0 and is not
# larger than the job's wall time. Passthrough/redact/truncate need no
# native companion.
# ---------------------------------------------------------------------------


def test_admitted_150k_row_job_elapsed_ms_is_positive_and_bounded_by_wall_time(
    tmp_path: Path,
) -> None:
    n_rows = 150_000
    idx = list(range(n_rows))
    source = pa.table(
        {
            "p": pa.array([f"val-{i}" for i in idx], type=pa.string()),
            "r": pa.array([f"secret-{i}" for i in idx], type=pa.string()),
            "tr": pa.array([f"code-{i:08d}" for i in idx], type=pa.string()),
        }
    )
    path = write_read_only_fixture(tmp_path, source, "big150k")
    columns = [
        {"name": "p", "strategy": "passthrough"},
        {"name": "r", "strategy": "redact"},
        {"name": "tr", "strategy": "truncate", "provider_config": {"length": 3}},
    ]
    config = build_config(tmp_path, "t", path, columns)

    t0 = time.perf_counter()
    result = run_pipeline(
        config,
        {"t": source},
        engine_version="unified-lane-timings-150k-test",
        key_provider=_key_provider(),
        auto_chunk=False,
        unified_slice_enabled=True,
    )
    wall_ms = (time.perf_counter() - t0) * 1000.0

    assert QUALITY_METRICS_KEY in result.quality_metrics, "expected an admitted job"
    assert result.timings, "expected per-node timing records for an admitted job"
    for record in result.timings:
        assert record.elapsed_ms >= 0
        assert record.peak_memory_delta_kb >= 0
    total_elapsed_ms = sum(record.elapsed_ms for record in result.timings)
    assert total_elapsed_ms > 0
    assert total_elapsed_ms <= wall_ms, (
        f"sum(elapsed_ms)={total_elapsed_ms:.3f} exceeds the job's own wall time={wall_ms:.3f}"
    )


# ---------------------------------------------------------------------------
# AT4: boundary_conversion_ms equals the exact accumulated value under a
# controlled clock, covering the admission conversion, the round-trip, and
# the output bridge.
# ---------------------------------------------------------------------------


class _FakeClock:
    """A monotonically-increasing stand-in for `time.perf_counter`, shared
    across `_unified_slice_admission` and `_unified_slice`'s own `time`
    bindings (patched per-module, never the real `time` module) so every
    boundary-conversion measurement in the admitted call advances the SAME
    counter in real call order, while every other `perf_counter` user in the
    process (in particular `instrumentation.timing`'s per-node stopwatch)
    keeps reading the real clock, untouched."""

    def __init__(self, values: list[float]) -> None:
        self._values = iter(values)

    def perf_counter(self) -> float:
        return next(self._values)


def test_boundary_conversion_ms_equals_admission_plus_bridge_under_controlled_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"p": pa.array(["a", "b", "c"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "single_passthrough")
    columns = [{"name": "p", "strategy": "passthrough"}]
    config = build_config(tmp_path, "t", path, columns)

    # 6 calls, in the real chronological order `_execute_admitted` makes them:
    # (admission to_pandas_fk_safe start/end), (admission round-trip
    # start/end), (output-bridge start/end). 1000ms + 1000ms + 3000ms.
    clock = _FakeClock([0.0, 1.0, 2.0, 3.0, 3.0, 6.0])
    monkeypatch.setattr(_unified_slice_admission, "time", clock)
    monkeypatch.setattr(_unified_slice, "time", clock)

    result = run_pipeline(
        config,
        {"t": source},
        engine_version="unified-lane-timings-boundary-ms-test",
        key_provider=_key_provider(),
        unified_slice_enabled=True,
    )

    assert QUALITY_METRICS_KEY in result.quality_metrics, "expected an admitted job"
    assert result.boundary_conversion_ms == pytest.approx(5000.0)
