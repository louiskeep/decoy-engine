"""Phase D / D1b-i: the per-column decision is pinned; a violation aborts, never switches.

A bucket_perturb column takes ONE backend for the whole run. A native column is validated
against its pinned source type on every batch, a missing pinned column is rejected rather than
skipped, a date the native operator cannot compute safely fails closed, and a kernel fault ends
the run with the sink aborted and nothing committed. `apply_bucket_perturb` is forbidden for any
column decided native, so "never half native" holds by construction. The config validation
stages keep their order: a config that errors today errors identically with the companion
present, and never reaches the kernel. Runs on the reference double, so it holds without the
companion.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._operator_registry import RUST_COMPANION
from decoy_engine.execution.out_of_core import _mask_group_c
from tests.parity._d1a_hash_data import run_chain
from tests.parity._d1b1_bucket_data import (
    ISO,
    MAIN_SEEDS,
    SHAPE_SEEDS,
    STAMP,
    PythonReferenceIndexKernel,
    boundary_column,
    bucket_seed,
    install_index_companion,
    main_payload,
    shape_payload,
)
from tests.parity._d1b1_bucket_expected import EXPECTED
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs
from tests.parity.test_phase_d_d1a_hash_pinning import (
    _DRIVERS,
    RecordingSink,
    _as_large,
    _assert_aborted,
    _without_a,
    mutate_second_batch,
)

_ROWS = pa.table(
    {"a": pa.array(["2024-03-05", "2023-07-19", "2022-01-01", "2021-05-05", "2020-02-29"])}
)
_SEEDS = (("a", bucket_seed("week")),)
KEY = "out_of_core_bucket_perturb"
_MODES = ["resident", "sink-batch", "sink-reorder"]
RANGE_CODE = "out_of_core_bucket_perturb_date_out_of_native_range"


def forbid_python_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("apply_bucket_perturb ran for a column decided native")

    monkeypatch.setattr(_mask_group_c, "apply_bucket_perturb", forbidden)


def entry(run: Any, column: str = "a") -> dict[str, Any]:
    (found,) = (e for e in run.result.quality_metrics[KEY] if e["column"] == column)
    return found


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (_as_large, "out_of_core_bucket_perturb_source_type_drift"),
        (_without_a, "out_of_core_bucket_perturb_column_missing"),
    ],
    ids=["type-drift", "missing-column"],
)
@pytest.mark.parametrize(("mode", "module"), _DRIVERS)
def test_a_pinned_column_that_changes_or_vanishes_aborts_the_sink(
    mode: str,
    module: Any,
    change: Callable[[pa.RecordBatch], pa.RecordBatch],
    code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_index_companion(monkeypatch)
    forbid_python_bucket(monkeypatch)
    mutate_second_batch(monkeypatch, module, change)
    out_dir = tmp_path / "out"
    sink = RecordingSink(out_dir)
    with pytest.raises(ExecutionError) as caught:
        run_chain(
            _ROWS,
            _SEEDS,
            sink=sink,
            out_dir=out_dir,
            batch_rows=2,
            reorder=mode.endswith("reorder"),
        )
    assert caught.value.code == code
    _assert_aborted(sink, out_dir)


@pytest.mark.parametrize(("mode", "module"), _DRIVERS)
def test_a_mid_run_kernel_fault_aborts_without_a_python_switch(
    mode: str, module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = PythonReferenceIndexKernel(fail_on_call=2)
    install_index_companion(monkeypatch, kernel)
    forbid_python_bucket(monkeypatch)
    out_dir = tmp_path / "out"
    sink = RecordingSink(out_dir)
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_chain(
            _ROWS,
            _SEEDS,
            sink=sink,
            out_dir=out_dir,
            batch_rows=2,
            reorder=mode.endswith("reorder"),
        )
    assert kernel.calls == 2  # the fault ended the run; nothing retried it on another path
    _assert_aborted(sink, out_dir)


def test_a_mid_run_kernel_fault_returns_no_partial_resident_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_index_companion(monkeypatch, PythonReferenceIndexKernel(fail_on_call=2))
    forbid_python_bucket(monkeypatch)
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_chain(_ROWS, _SEEDS, batch_rows=2)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("batch_rows", [None, 1, 7, 16])
def test_per_batch_equals_whole_column_and_python_never_runs(
    mode: str, batch_rows: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    forbid_python_bucket(monkeypatch)
    run = run_chain(
        main_payload(), MAIN_SEEDS, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path)
    )
    for name, _ in MAIN_SEEDS:
        assert run.tables["t"].column(name).to_pylist() == EXPECTED[name], name
        assert run.tables["t"].column(name).type == pa.string(), name
        assert entry(run, name)["executed_backend"] == RUST_COMPANION, name


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("batch_rows", [None, 5, 1])
def test_per_batch_shape_reconciles_to_the_pinned_type_and_output(
    mode: str, batch_rows: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    forbid_python_bucket(monkeypatch)
    run = run_chain(
        shape_payload(), SHAPE_SEEDS, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path)
    )
    for name, _ in SHAPE_SEEDS:
        assert run.tables["t"].column(name).to_pylist() == EXPECTED[name], name
    if mode == "resident":
        # Data-derived like the pandas path: an all-null column is null-typed, an
        # all-unparseable-but-present one stays string.
        assert run.tables["t"].column("b_allnull").type == pa.null()
        assert run.tables["t"].column("b_unparse").type == pa.string()


# --- Empty, all-null and all-unparseable boundary: zero kernel calls, the column history counts ---


def _dates(values: list[str | None]) -> pa.Table:
    return pa.table({"a": pa.array(values, pa.string())})


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    "values",
    [[None] * 5, ["nope", "bad", "2024-99-99", "", "x"]],
    ids=["all-null", "all-unparseable"],
)
def test_a_column_with_nothing_to_perturb_makes_no_kernel_call_and_reports_idle(
    values: list[str | None], mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_index_companion(monkeypatch)
    forbid_python_bucket(monkeypatch)
    run = run_chain(_dates(values), _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    assert state["kernel"].calls == 0
    assert run.tables["t"].column("a").to_pylist() == values
    assert entry(run) == {
        "table": "t",
        "column": "a",
        "path": "mask_batch",
        "executed_backend": "none",
        "reason": "no_native_kernel_calls",
    }


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    "values",
    [
        ["2024-03-05", "2024-03-06", None, None, None, None],
        [None, None, "2024-03-05", "2024-03-06", None, None],
        [None, None, None, None, "2024-03-05", "2024-03-06"],
        ["2024-03-05", "2024-03-06", "nope", "bad", "", "x"],
        ["nope", "bad", "", "x", "2024-03-05", "2024-03-06"],
    ],
    ids=[
        "populated-then-null",
        "null-between-populated",
        "null-then-populated",
        "populated-then-unparseable",
        "unparseable-then-populated",
    ],
)
def test_idle_batches_neither_erase_nor_fake_a_native_execution(
    values: list[str | None], mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_index_companion(monkeypatch)
    forbid_python_bucket(monkeypatch)
    payload = _dates(values)
    run = run_chain(payload, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    # Every batch holding a parseable value makes one call (week buckets share one size).
    assert state["kernel"].calls == 1
    got = run.tables["t"].column("a").to_pylist()
    assert [v is None for v in got] == [v is None for v in values]
    assert [g for g, v in zip(got, values, strict=True) if v in (None, "nope", "bad", "", "x")] == [
        v for v in values if v in (None, "nope", "bad", "", "x")
    ]
    assert entry(run)["executed_backend"] == RUST_COMPANION
    assert "reason" not in entry(run)


def test_an_empty_table_makes_no_kernel_call(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_index_companion(monkeypatch)
    run = run_chain(_ROWS.slice(0, 0), _SEEDS)
    assert state["kernel"].calls == 0
    assert run.tables["t"].num_rows == 0


@pytest.mark.parametrize("mode", ["sink-batch", "sink-reorder"])
def test_lazy_sources_bind_the_prep_time_type_to_the_streamed_type(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    forbid_python_bucket(monkeypatch)
    lazy = tmp_path / "src"
    lazy.mkdir()
    run = run_chain(
        pa.table({"b_month": main_payload().column("b_month")}),
        (("b_month", bucket_seed("month")),),
        lazy_dir=lazy,
        batch_rows=7,
        **mode_kwargs(mode, tmp_path),
    )
    assert run.tables["t"].column("b_month").to_pylist() == EXPECTED["b_month"]
    assert entry(run, "b_month")["executed_backend"] == RUST_COMPANION


# --- Date-range guard: fail closed, never consume a possibly wrapped result ---


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("batch_rows", [None, 2])
def test_a_date_near_the_pandas_range_bound_fails_closed(
    mode: str, batch_rows: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    forbid_python_bucket(monkeypatch)
    seeds = (("b_boundary", bucket_seed("week", STAMP)),)
    out_dir = tmp_path / "out"
    kwargs: dict[str, Any] = {}
    sink = None
    if mode != "resident":
        sink = RecordingSink(out_dir)
        kwargs = {"sink": sink, "out_dir": out_dir, "reorder": mode.endswith("reorder")}
    with pytest.raises(ExecutionError) as caught:
        run_chain(
            pa.table({"b_boundary": boundary_column()}), seeds, batch_rows=batch_rows, **kwargs
        )
    assert caught.value.code == RANGE_CODE
    assert "b_boundary" in str(caught.value)
    if sink is not None:
        _assert_aborted(sink, out_dir)


def test_out_of_range_dates_are_not_near_the_bound_and_pass_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """9999-12-31 and 2299-01-01 parse to NaT on both paths, so they need no guard."""
    install_index_companion(monkeypatch)
    forbid_python_bucket(monkeypatch)
    values = ["9999-12-31", "2299-01-01", "2024-03-05"]
    run = run_chain(_dates(values), (("a", bucket_seed("quarter", ISO)),))
    got = run.tables["t"].column("a").to_pylist()
    assert got[:2] == values[:2]
    assert got[2] != values[2]


# --- Validation ordering: native selection never hoists or replaces an existing check ---


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("seed", "code"),
    [
        (bucket_seed("year"), "bucket_perturb_invalid_config"),
        (bucket_seed("month", None), "out_of_core_bucket_perturb_autodetect_unsupported"),
        (bucket_seed("month", ""), "out_of_core_bucket_perturb_autodetect_unsupported"),
        (bucket_seed("month", "%z"), "bucket_perturb_invalid_config"),
        (bucket_seed("month", "%H:%M"), "bucket_perturb_invalid_config"),
        (bucket_seed("month", "mixed"), "bucket_perturb_invalid_config"),
        (bucket_seed("month", "ISO8601"), "bucket_perturb_invalid_config"),
        (bucket_seed("month", "123"), "bucket_perturb_invalid_config"),
        (bucket_seed("month", "%H:%M %z"), "bucket_perturb_invalid_config"),
        (bucket_seed("month", namespace=None), "bucket_perturb_requires_namespace"),
    ],
    ids=[
        "unsupported-bucket",
        "missing-format",
        "empty-format",
        "tz-alone",
        "time-only",
        "mixed",
        "iso8601",
        "numeric",
        "tz-on-invalid",
        "no-namespace",
    ],
)
def test_config_errors_are_unchanged_with_the_companion_present_and_never_reach_the_kernel(
    seed: Any, code: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_index_companion(monkeypatch)
    out_dir = tmp_path / "out"
    with pytest.raises((ExecutionError, StrategyError)) as caught:
        run_chain(
            _dates(["2024-03-05", None]),
            (("a", seed),),
            **(
                {}
                if mode == "resident"
                else {"out_dir": out_dir, "reorder": mode.endswith("reorder")}
            ),
        )
    assert caught.value.code == code  # type: ignore[attr-defined]
    assert state["kernel"].calls == 0
    assert not out_dir.exists()
