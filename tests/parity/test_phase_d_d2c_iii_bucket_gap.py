"""Phase D / D2c-iii gap closure: behaviors a mutation run showed no test pinned.

1. A one-row and a zero-row native bucket column.
2. The exact reach boundary of the native-range predicate, and that it honors `date_format`.
3. The thread budget reaching the index kernel.
4. The extension-dtype branch of the range predicate.

Stand-in kernels only; no companion is needed.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import _sequential_native
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._sequential_native import SeqNativeRun
from decoy_engine.execution.native._bucket_perturb_ext import native_range_exceeded
from tests.parity._d1a_hash_data import JOB_SEED, column_seed
from tests.parity._d2b_shapes import run_full, run_seq, strs
from tests.parity._d2c_bucket_data import (
    DMY,
    ISO,
    BucketMarkingKernel,
    ReferenceBucketKernel,
    bucket_payload_shape,
    bucket_seed,
    expect_last_offset,
    install_bucket,
)

NATIVE = "rust_companion"
PYTHON = "python_scalar"
DISPOSITION = "sequential_bucket_perturb"
STAMP_US = "%Y-%m-%d %H:%M:%S.%f"


def _entries(result: Any) -> list[dict[str, Any]]:
    return result.quality_metrics[DISPOSITION]


def _values(*cells: str | None) -> pa.Array:
    return pa.array(list(cells), pa.string())


# One-row and zero-row frames.


def test_a_one_row_frame_runs_natively_and_consumes_the_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills bucket_column `rows == 0` -> `rows == 1`: a 1-row frame must reach the kernel."""
    kernel = install_bucket(monkeypatch, ReferenceBucketKernel())
    shape = bucket_payload_shape({"b": (strs(["2024-03-05"]), bucket_seed("month"))})
    result = run_seq(shape)
    [entry] = _entries(result)
    assert [c.rows for c in kernel.calls] == [1]
    assert entry["executed_backend"] == NATIVE and "reason" not in entry
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


def test_a_one_row_frame_takes_the_kernel_result_not_the_python_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills the same `rows == 1` mutant from the output side: the marker proves consumption."""
    install_bucket(monkeypatch, BucketMarkingKernel())
    out = run_seq(bucket_payload_shape({"b": (strs(["2024-03-05"]), bucket_seed("month"))}))
    assert out.outputs["P"].column("b").to_pylist() == expect_last_offset(
        ["2024-03-05"], "month", ISO
    )


def test_a_zero_row_frame_never_reaches_the_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills `or bound is None` -> `and bound is None`: with a bound kernel present, only the
    row-count test keeps an empty selection away from it."""
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    run = SeqNativeRun()
    ctx = _ctx()
    _decide(run, seed=bucket_seed("month"))
    out = run.bucket_column(
        ctx,
        pd.DataFrame({"x": pd.Series([], dtype=object)}),
        "x",
        namespace="n",
        mask_key=JOB_SEED,
        bucket="month",
        date_format=ISO,
        preserve=None,
    )
    assert out is None
    assert kernel.calls == []
    decision = run._decisions[("t", "x")]
    assert decision.rows == 0 and decision.native_calls == 0


def test_an_empty_selection_through_the_route_records_the_skip_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    shape = bucket_payload_shape(
        {"b": (strs(["2024-03-05", "2023-07-19"]), bucket_seed("month"))},
        extra_cols={"flag": pa.array([0, 0], pa.int64())},
        extra_seeds={"flag": column_seed("passthrough", namespace=None)},
        when={"b": "flag == 1"},
    )
    [entry] = _entries(run_seq(shape))
    assert kernel.calls == []
    assert entry["executed_backend"] == "none" and entry["reason"] == "no_rows_hashed"


# The reach boundary.


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        # Pandas min is 1677-09-21 00:12:43.145224193; a month reaches 32 days. The cells sit
        # one microsecond either side of min + 32d (1677-10-23 00:12:43.145224193).
        ("1677-10-23 00:12:43.145224", True),
        ("1677-10-23 00:12:43.145225", False),
        # Pandas max is 2262-04-11 23:47:16.854775807; max - 32d is 2262-03-10 23:47:16.854775807.
        ("2262-03-10 23:47:16.854775", False),
        ("2262-03-10 23:47:16.854776", True),
    ],
)
def test_the_month_reach_boundary_is_exact_to_the_microsecond(cell: str, expected: bool) -> None:
    """Pins `<` / `>` and the 31 + 1 day reach: one step inside is native, one step past is not.

    A string cell carries at most microseconds while both thresholds end in non-zero
    nanoseconds, so `value == threshold` cannot occur and `<` vs `<=` is unobservable through
    this predicate; the microsecond pair is the tightest pin available."""
    assert native_range_exceeded(_values(cell), "month", STAMP_US) is expected


@pytest.mark.parametrize(
    ("cell", "expected"),
    [("1677-10-23", True), ("1677-10-24", False), ("2262-03-10", False), ("2262-03-11", True)],
)
def test_the_seam_declines_one_day_past_the_reach_and_runs_natively_at_it(
    cell: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_bucket(monkeypatch, ReferenceBucketKernel())
    shape = bucket_payload_shape({"b": (strs([cell, "2024-03-05"]), bucket_seed("month"))})
    result = run_seq(shape)
    [entry] = _entries(result)
    if expected:
        assert kernel.calls == []
        assert entry["executed_backend"] == PYTHON
        assert entry["reason"] == "sequential_bucket_perturb_date_out_of_native_range"
    else:
        assert kernel.calls
        assert entry["executed_backend"] == NATIVE
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


# The configured date_format is what the range check parses with.

# Day-first 10 April 2262 is inside the reach of the pandas max. Parsed without the format,
# pandas reads it month-first (10 April -> October 4th, 2262), which is past the range, so it
# is NaT and nothing is flagged.
DMY_NEAR_MAX = "10/04/2262"


def test_the_range_predicate_parses_with_the_configured_format() -> None:
    """Kills the dropped-`date_format` mutants of `native_range_exceeded`."""
    cells = _values(DMY_NEAR_MAX)
    assert native_range_exceeded(cells, "month", DMY) is True
    assert pd.isna(pd.to_datetime(cells.to_pandas(), errors="coerce").max())


def test_the_seam_range_check_uses_the_configured_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills the dropped-`date_format` mutant in the seam's call to the predicate."""
    kernel = install_bucket(monkeypatch, ReferenceBucketKernel())
    shape = bucket_payload_shape(
        {"b": (strs([DMY_NEAR_MAX, "25/12/2023"]), bucket_seed("month", DMY))}
    )
    result = run_seq(shape)
    [entry] = _entries(result)
    assert kernel.calls == []
    assert entry["executed_backend"] == PYTHON
    assert entry["reason"] == "sequential_bucket_perturb_date_out_of_native_range"
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


def test_the_out_of_core_guard_uses_the_configured_format() -> None:
    """Kills the dropped-`date_format` mutant in `_guard_native_range`."""
    from decoy_engine.execution._errors import ExecutionError
    from decoy_engine.execution.out_of_core import _bucket_native

    with pytest.raises(ExecutionError):
        _bucket_native._guard_native_range(_values(DMY_NEAR_MAX), "month", DMY, "d")


# The thread budget.


def _ctx(table: str = "t") -> StrategyContext:
    ctx = object.__new__(StrategyContext)
    object.__setattr__(ctx, "current_table", table)
    object.__setattr__(ctx, "nested_outer_column", "")
    return ctx


def _decide(run: SeqNativeRun, seed: Any, col: str = "x") -> None:
    run._decisions[("t", col)] = run._decide(
        _sequential_native._OPS["bucket_perturb"], "t", col, pa.string(), seed
    )


def test_the_thread_budget_reaches_the_index_kernel_through_a_bucket_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills the dropped / None `native_threads` mutants in `_load_bucket` and `_bucket_apply`."""
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    run = SeqNativeRun(native_threads=3)
    _decide(run, bucket_seed("month"))
    assert run._bound["bucket_perturb"].native_threads == 3
    out = run.bucket_column(
        _ctx(),
        pd.DataFrame({"x": ["2024-03-05", "2024-02-10"]}),
        "x",
        namespace="n",
        mask_key=JOB_SEED,
        bucket="month",
        date_format=ISO,
        preserve=None,
    )
    assert out is not None
    assert kernel.threads and set(kernel.threads) == {3}


# The extension-dtype branch of the range predicate.


class _ExtensionBacked:
    """Quacks like the Arrow array the predicate reads: `to_pandas()` yields a nullable-string
    extension series, the shape an Arrow-backed pandas conversion produces."""

    def __init__(self, cells: list[str | None]) -> None:
        self._cells = cells

    def to_pandas(self) -> pd.Series:
        return pd.Series(self._cells, dtype="string[python]")


@pytest.mark.parametrize(("cell", "expected"), [("2262-03-11", True), ("2024-03-05", False)])
def test_the_range_predicate_agrees_across_object_and_extension_dtypes(
    cell: str, expected: bool
) -> None:
    """Kills the extension-dtype branch mutants: the `astype(object)` coercion must run for an
    extension series and give the same verdict as the object-dtype path."""
    backed = _ExtensionBacked([cell, None])
    assert pd.api.types.is_extension_array_dtype(backed.to_pandas().dtype)
    assert native_range_exceeded(backed, "month", ISO) is expected
    assert native_range_exceeded(_values(cell, None), "month", ISO) is expected
