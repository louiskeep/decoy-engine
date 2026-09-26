"""Acceptance: native deterministic date_shift (v1: explicit format, string
source, no group_by, full-frame only).

Byte parity (value AND Arrow field type) against the pinned pandas oracle is the
merge gate, proven at three boundaries: the operator itself vs
`DateShiftStrategyHandler.run` (values AND the RowError set), the shadow
coordinator's assembled output vs the oracle `run_pipeline`, and the production
unified-slice `ExecutionResult` (flag-off vs flag-on).

Row errors: a non-null value that fails the explicit format is a `format_error`.
The coordinator collects the operator's batch-local records, rebases them to
table-global indices, and attributes the table. In production a non-empty set
raises `RowErrorsFailedError` in finalize, the unified slice reroutes the table to
the oracle, and the oracle fails identically. The native records are asserted
directly on `ShadowRunResult.row_errors` (BEFORE any reroute, where the oracle
would recompute them), and a guard proves that dropping them would let the raw
value through with the job succeeding.

Native-path tests need the compiled companion and skip without it; the
companion-absent production decline is covered by the run_pipeline tests.
"""

from __future__ import annotations

import dataclasses
import functools
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.determinism import derive
from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import _unified_slice_admission as admission
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution._pipeline_finalize import finalize_validators_and_quarantine
from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.execution._strategies._date_shift import DateShiftStrategyHandler
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution.native._companion_status import (
    KernelAvailability,
    native_companion_status,
)
from decoy_engine.execution.native._date_shift_ext import FORMAT_ERROR_REASON, native_date_shift
from decoy_engine.execution.native._dispatch import plan_native_route
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.execution.native._plan import native_route_eligibility
from decoy_engine.execution.physical import _shadow_coordinator
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._plan import ExecutionBinding, KeyBinding
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence, run_operator
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.plan._types import ColumnSeed
from tests.physical import _shadow_helpers
from tests.physical._shadow_helpers import (
    ENGINE_VERSION,
    assert_every_node_bound,
    assert_route_evidence_matches_plan,
    assert_shadow_matches_oracle,
    build_config,
    run_shadow_and_oracle,
    write_read_only_fixture,
)

_MASK_KEY = bytes(range(32))
_FMT = "%Y-%m-%d"
_KAT_PATH = Path(__file__).resolve().parents[1] / "vectors" / "date_shift_kat.json"

_NEEDS_COMPANION = pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable; the native shadow path requires it",
)


@pytest.fixture(autouse=True)
def _pandas_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_pipeline` defaults to `unified_slice_enabled=True`, so the shared
    harness's "oracle" call would itself take the native lane for an admitted
    date_shift job and compare native against native. Pin it to the legacy
    pandas route so every coordinator-vs-oracle assertion here is real."""
    monkeypatch.setattr(
        _shadow_helpers,
        "run_pipeline",
        functools.partial(run_pipeline, unified_slice_enabled=False),
    )


def _kp() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


def _ds_column(
    pc: dict[str, Any] | None = None,
    *,
    date_format: str | None = _FMT,
    namespace: str | None = "ns",
) -> dict[str, Any]:
    provider_config: dict[str, Any] = dict(pc) if pc is not None else {}
    if date_format is not None:
        provider_config.setdefault("date_format", date_format)
    col: dict[str, Any] = {
        "name": "c",
        "strategy": "date_shift",
        "provider_config": provider_config,
    }
    if namespace is not None:
        col["namespace"] = namespace
    return col


def _oracle_handler(
    values: list[Any], cfg: dict[str, Any], *, namespace: str = "ns"
) -> tuple[list[Any], list[tuple[int, str, str]]]:
    """Run the pinned oracle handler directly: (output values, row errors)."""
    seed = ColumnSeed(
        namespace=namespace,
        strategy="date_shift",
        provider=None,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        provider_config=tuple(sorted(cfg.items())),
    )
    ctx = SimpleNamespace(
        mask_key=_MASK_KEY, row_errors=[], group_anchor_snapshots={}, current_table="t"
    )
    df, _ = DateShiftStrategyHandler().run(
        pd.DataFrame({"c": pd.Series(values, dtype=object)}), "c", seed, ctx
    )
    return list(df["c"]), [(e.row_index, e.trigger, e.reason) for e in ctx.row_errors]


def _native(
    values: list[Any],
    *,
    min_days: int = -365,
    max_days: int = 365,
    fmt: str = _FMT,
    namespace: str = "ns",
    native_threads: int | None = None,
) -> tuple[list[Any], tuple[int, ...]]:
    out, errors = native_date_shift(
        pa.array(values, type=pa.string()),
        min_days=min_days,
        max_days=max_days,
        date_format=fmt,
        mask_key=_MASK_KEY,
        namespace=namespace,
        index_kernel=load_compiled_index_kernel(),
        native_threads=native_threads,
    )
    return out.to_pylist(), errors


# ── Operator differential vs the oracle handler (values + row errors) ──

_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%Y%m%d", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S", "%d-%b-%Y", "%j-%Y"]
_RANGES = [(-365, 365), (0, 0), (-1, 1), (30, -30), (1, 1000), (-3650, -3000)]
_BASE_DATES = [
    "2024-02-29",
    "2023-02-28",
    "2024-01-01",
    "2024-12-31",
    "2000-02-29",
    "1999-11-15",
    "1970-01-01",
    "2038-01-19",
]


@_NEEDS_COMPANION
@pytest.mark.parametrize("fmt", _FORMATS)
@pytest.mark.parametrize("bounds", _RANGES, ids=[f"{a}..{b}" for a, b in _RANGES])
def test_operator_matches_oracle_handler(fmt: str, bounds: tuple[int, int]) -> None:
    """Values AND the exact RowError set (index, trigger, reason) match the
    oracle, over parseable, duplicate, null, and parse-failing rows."""
    lo, hi = bounds
    base = [pd.Timestamp(d).strftime(fmt) for d in _BASE_DATES]
    values = [*base, base[0], None, "not-a-date", "", "café", None, "2024-13-45"]
    native_out, native_errors = _native(values, min_days=lo, max_days=hi, fmt=fmt)
    oracle_out, oracle_errors = _oracle_handler(
        values, {"date_format": fmt, "min_days": lo, "max_days": hi}
    )
    assert native_out == oracle_out
    assert [(i, "format_error", FORMAT_ERROR_REASON) for i in native_errors] == oracle_errors
    # The error set is non-trivial and excludes source-null rows.
    assert oracle_errors and all(values[i] is not None for i, _, _ in oracle_errors)


@_NEEDS_COMPANION
def test_operator_parse_strictness_matches_oracle() -> None:
    corpus = [
        "2024-1-1",
        "2024-01-1",
        " 2024-01-01",
        "2024-01-01 ",
        "2024/01/01",
        "2024-00-10",
        "2024-01-00",
        "2024-02-30",
        "0001-01-01",
        "1677-09-21",
        "2262-04-11",
        "9999-12-31",
        "2024-01-01x",
        "not",
        "",
        "   ",
        None,
        "2024-01-01",
    ]
    native_out, native_errors = _native(corpus, min_days=0, max_days=0)
    oracle_out, oracle_errors = _oracle_handler(
        corpus, {"date_format": _FMT, "min_days": 0, "max_days": 0}
    )
    assert native_out == oracle_out
    assert [i for i, _, _ in oracle_errors] == list(native_errors)


@_NEEDS_COMPANION
def test_operator_default_bounds_match_oracle_defaults() -> None:
    """An omitted min/max resolves to the oracle's (-365, 365) at binding; the
    operator given those defaults matches the oracle run with NO bounds."""
    values = ["2024-05-15", "2023-08-20", None, "bad"]
    assert _native(values)[0] == _oracle_handler(values, {"date_format": _FMT})[0]


@_NEEDS_COMPANION
def test_operator_thread_invariant() -> None:
    values = [f"20{y:02d}-0{m}-1{d}" for y in range(30) for m in range(1, 10) for d in range(10)]
    one = _native([*values, None, "bad"], native_threads=1)
    four = _native([*values, None, "bad"], native_threads=4)
    assert one == four


@_NEEDS_COMPANION
def test_operator_accepts_chunked_array() -> None:
    kernel = load_compiled_index_kernel()
    chunked = pa.chunked_array([["2024-01-01", None], ["bad", "2023-06-30"]], type=pa.string())
    out, errors = native_date_shift(
        chunked,
        min_days=-365,
        max_days=365,
        date_format=_FMT,
        mask_key=_MASK_KEY,
        namespace="ns",
        index_kernel=kernel,
    )
    oracle_out, oracle_errors = _oracle_handler(
        ["2024-01-01", None, "bad", "2023-06-30"], {"date_format": _FMT}
    )
    assert out.to_pylist() == oracle_out
    assert [i for i, _, _ in oracle_errors] == list(errors) == [2]


# ── KAT: offset == derive_index_batch(source, range_size) + min_days ──


def _kat() -> dict[str, Any]:
    return json.loads(_KAT_PATH.read_text(encoding="utf-8"))


def _resolved(case: dict[str, Any]) -> tuple[int, int]:
    lo = -365 if case["min_days"] is None else case["min_days"]
    hi = 365 if case["max_days"] is None else case["max_days"]
    return (hi, lo) if lo > hi else (lo, hi)


def test_kat_reference_offsets_reproduce() -> None:
    """Pure-Python: the pinned offsets are the oracle's own formula (no companion)."""
    kat = _kat()
    key = bytes.fromhex(kat["mask_key_hex"])
    for case in kat["cases"]:
        lo, hi = _resolved(case)
        got = [
            lo
            + int.from_bytes(derive(key, kat["namespace"], _canonicalize_source(v))[:8], "big")
            % (hi - lo + 1)
            for v in case["source"]
        ]
        assert got == case["expected_offsets"]


@_NEEDS_COMPANION
def test_kat_derive_index_batch_plus_min_days() -> None:
    """The operator reduction: `derive_index_batch(source, range_size) + min_days`."""
    kat = _kat()
    kernel = load_compiled_index_kernel()
    for case in kat["cases"]:
        lo, hi = _resolved(case)
        idx = kernel.derive_index_batch(
            pa.array(case["source"], type=pa.string()),
            mask_key=bytes.fromhex(kat["mask_key_hex"]),
            namespace=kat["namespace"],
            pool_size=hi - lo + 1,
        )
        assert [i + lo for i in idx.to_pylist()] == case["expected_offsets"]


@_NEEDS_COMPANION
def test_kat_native_output() -> None:
    kat = _kat()
    kernel = load_compiled_index_kernel()
    for case in kat["cases"]:
        out, errors = native_date_shift(
            pa.array(case["source"], type=pa.string()),
            min_days=-365 if case["min_days"] is None else case["min_days"],
            max_days=365 if case["max_days"] is None else case["max_days"],
            date_format=case["date_format"],
            mask_key=bytes.fromhex(kat["mask_key_hex"]),
            namespace=kat["namespace"],
            index_kernel=kernel,
        )
        assert out.to_pylist() == case["expected_output"]
        assert errors == ()


# ── Determinism / joinability ────────────────────────────────────────


@_NEEDS_COMPANION
def test_same_source_date_same_offset_across_batches(tmp_path: Path) -> None:
    """A repeated source date gets the identical shifted value wherever it lands
    (different batches, batch_size_rows=2), so same-value joins survive; a
    different namespace moves it."""
    values = ["2024-03-10", "2021-07-01", "2024-03-10", "1999-01-01", "2021-07-01", "2024-03-10"]
    source = pa.table({"c": pa.array(values, type=pa.string())})
    write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", tmp_path / "ds.parquet", [_ds_column()])
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp(), batch_size_rows=2)
    assert_shadow_matches_oracle(run)
    out = run.shadow.outputs["t"].column("c").to_pylist()
    assert out[0] == out[2] == out[5]
    assert out[1] == out[4]
    other_ns, _ = _native(values, namespace="other_ns")
    assert other_ns != out


# ── Coordinator vs oracle (clean shapes: no parse failures) ──────────

_SHAPES: list[tuple[str, list[Any]]] = [
    ("populated", ["2024-05-15", "2023-08-20", "2021-01-10", "2024-11-30"]),
    ("leap_boundary", ["2024-02-29", "2023-02-28", "2000-02-29", "2024-12-31", "2024-01-01"]),
    ("with_nulls", ["2024-02-15", None, "2023-11-30", None]),
    ("dup", ["2024-01-01", "2024-01-01", None, "2024-01-01"]),
    ("single", ["2024-07-04"]),
    ("empty", []),
    ("all_null", [None, None, None]),
]


@_NEEDS_COMPANION
@pytest.mark.parametrize("batch_size_rows", [2, 50_000])
@pytest.mark.parametrize("label, values", _SHAPES, ids=[s[0] for s in _SHAPES])
def test_coordinator_matches_oracle_byte_identical(
    tmp_path: Path, label: str, values: list[Any], batch_size_rows: int
) -> None:
    source = pa.table({"c": pa.array(values, type=pa.string())})
    write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", tmp_path / "ds.parquet", [_ds_column()])
    run = run_shadow_and_oracle(
        config, "t", source, key_provider=_kp(), batch_size_rows=batch_size_rows
    )
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)  # value + null + order + rows + field type + row_errors
    assert_route_evidence_matches_plan(run)
    assert run.shadow.row_errors == ()


@_NEEDS_COMPANION
def test_assembled_type_per_shape(tmp_path: Path) -> None:
    """The tokenizing null-shape mapping, pinned concretely (not just shadow ==
    oracle): value-bearing -> string, empty -> float64, all-null -> null."""
    expected = {"populated": pa.string(), "empty": pa.float64(), "all_null": pa.null()}
    for label, values in _SHAPES:
        if label not in expected:
            continue
        source = pa.table({"c": pa.array(values, type=pa.string())})
        sub = tmp_path / label
        sub.mkdir()
        write_read_only_fixture(sub, source, "ds")
        config = build_config(sub, "t", sub / "ds.parquet", [_ds_column()])
        run = run_shadow_and_oracle(config, "t", source, key_provider=_kp())
        assert run.shadow.outputs["t"].schema.field("c").type == expected[label]
        assert run.oracle.outputs["t"].schema.field("c").type == expected[label]


# ── Native-route-taken proof (success case, multi-batch) ─────────────


@_NEEDS_COMPANION
def test_native_route_taken_multi_batch(tmp_path: Path) -> None:
    values = ["2024-02-15", "2023-11-30", None, "2021-06-01", "2024-02-15"]
    source = pa.table({"c": pa.array(values, type=pa.string())})
    write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", tmp_path / "ds.parquet", [_ds_column()])
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp(), batch_size_rows=2)
    (node,) = [n for t in run.plan.tables for n in t.nodes]
    assert node.execution is not None
    assert node.execution.operator_id == "native_date_shift"
    assert node.execution.pool_binding is None
    assert node.execution.needs_index_kernel is True
    (evidence,) = run.shadow.route_evidence.values()
    assert evidence.actual_operator == "native_date_shift"
    assert evidence.executed is True
    assert evidence.compiled_kernel_executed is True
    assert evidence.batches_run == 3
    assert_shadow_matches_oracle(run)


@_NEEDS_COMPANION
def test_date_shift_node_loads_kernel_without_pool_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kernel-load and pool-resolution are separate conditions: a date_shift node
    loads the index kernel exactly once and never reaches `_resolve_pool`."""
    loads: list[int] = []
    real_load = _shadow_coordinator.load_compiled_index_kernel

    def _counting_load() -> Any:
        loads.append(1)
        return real_load()

    def _no_pool(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("pool resolution must not run for a date_shift node")

    monkeypatch.setattr(_shadow_coordinator, "load_compiled_index_kernel", _counting_load)
    monkeypatch.setattr(ShadowCoordinator, "_resolve_pool", _no_pool)
    source = pa.table({"c": pa.array(["2024-01-01", "2023-01-01", "2022-01-01"], type=pa.string())})
    write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", tmp_path / "ds.parquet", [_ds_column()])
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp(), batch_size_rows=1)
    assert loads == [1]
    assert_shadow_matches_oracle(run)


# ── Failure parity + cross-batch table-global index ──────────────────

_ERR_VALUES = ["2024-01-01", "2023-05-05", "2022-02-02", "not-a-date", None, "2021-12-31"]
_ERR_RECORD = RowErrorRecord(
    table="t", column="c", row_index=3, trigger="format_error", reason=FORMAT_ERROR_REASON
)


def _shadow_only(config: dict[str, Any], source: pa.Table, *, batch_size_rows: int) -> Any:
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version=ENGINE_VERSION)
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=_kp(), batch_size_rows=batch_size_rows
    )
    snapshot = capture_shadow_snapshot({"t": source})
    return ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)


def _run(config: dict[str, Any], path: Path, *, flag: bool) -> Any:
    return run_pipeline(
        config,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_kp(),
        unified_slice_enabled=flag,
    )


@_NEEDS_COMPANION
def test_native_row_errors_are_table_global_before_reroute(tmp_path: Path) -> None:
    """The NATIVE `ShadowRunResult.row_errors` carries the table-global index
    (row 3 sits in the SECOND batch of 2) with the table attributed -- asserted
    on the coordinator's own result, before any oracle reroute could recompute
    it -- and equals the oracle's failure records exactly."""
    source = pa.table({"c": pa.array(_ERR_VALUES, type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", path, [_ds_column()])
    shadow = _shadow_only(config, source, batch_size_rows=2)
    assert shadow.row_errors == (_ERR_RECORD,)
    with pytest.raises(RowErrorsFailedError) as oracle_exc:
        _run(config, path, flag=False)
    assert tuple(oracle_exc.value.records) == shadow.row_errors


@_NEEDS_COMPANION
def test_production_format_error_reroutes_and_fails_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag-on: the native coordinator runs (batch 2), yields the table-global
    record, finalize raises, the slice reroutes, and the job fails with the
    SAME records as flag-off. The raw value is in no output (none is returned)."""
    captured: list[Any] = []
    real_run = ShadowCoordinator.run
    real_from_kp = ShadowContext.from_key_provider.__func__  # type: ignore[attr-defined]

    def _spy_run(self: ShadowCoordinator, *a: Any, **k: Any) -> Any:
        result = real_run(self, *a, **k)
        captured.append(result)
        return result

    def _small_batches(cls: Any, *a: Any, **k: Any) -> Any:
        k["batch_size_rows"] = 2
        return real_from_kp(cls, *a, **k)

    monkeypatch.setattr(ShadowCoordinator, "run", _spy_run)
    monkeypatch.setattr(ShadowContext, "from_key_provider", classmethod(_small_batches))
    source = pa.table({"c": pa.array(_ERR_VALUES, type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", path, [_ds_column()])
    with pytest.raises(RowErrorsFailedError) as on_exc:
        _run(config, path, flag=True)
    assert len(captured) == 1, "the native coordinator must have run before the reroute"
    assert captured[0].row_errors == (_ERR_RECORD,)
    monkeypatch.undo()
    with pytest.raises(RowErrorsFailedError) as off_exc:
        _run(config, path, flag=False)
    assert tuple(on_exc.value.records) == tuple(off_exc.value.records) == (_ERR_RECORD,)


@_NEEDS_COMPANION
def test_dropping_row_errors_would_leak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: with the coordinator's row-error collection defeated,
    the native path SUCCEEDS and the unparseable raw value reaches the output.
    The collection is what forces the fail-closed outcome."""
    real = _shadow_coordinator.run_operator

    def _drop_errors(*a: Any, **k: Any) -> Any:
        out, _ = real(*a, **k)
        return out, ()

    source = pa.table({"c": pa.array(_ERR_VALUES, type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", path, [_ds_column()])
    with pytest.raises(RowErrorsFailedError):
        _run(config, path, flag=True)
    monkeypatch.setattr(_shadow_coordinator, "run_operator", _drop_errors)
    leaked = _run(config, path, flag=True)
    assert QUALITY_METRICS_KEY in leaked.quality_metrics
    assert "not-a-date" in leaked.outputs["t"].column("c").to_pylist()


@_NEEDS_COMPANION
def test_error_bearing_main_output_matches_oracle_after_quarantine(tmp_path: Path) -> None:
    """Main-output byte parity on an ERROR-BEARING column: the shadow output run
    through the same quarantine finalize the oracle uses equals the oracle's
    quarantined output (value + type), and the removed row is the error row."""
    source = pa.table({"c": pa.array(_ERR_VALUES, type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", path, [_ds_column()])
    config["quarantine"] = {
        "enabled": True,
        "triggers": ["format_error"],
        "output_path": str(tmp_path / "q.jsonl"),
    }
    shadow = _shadow_only(config, source, batch_size_rows=2)
    oracle = _run(config, path, flag=False)
    assert tuple(oracle.row_errors) == shadow.row_errors == (_ERR_RECORD,)
    shadow_out, removed = finalize_validators_and_quarantine(
        dict(shadow.outputs),
        config=config,
        caller_sources={"t": source},
        mask_row_errors=shadow.row_errors,
        quality_metrics={},
    )
    assert removed == {"t": {3}}
    got, want = shadow_out["t"], oracle.outputs["t"]
    assert got.column("c").to_pylist() == want.column("c").to_pylist()
    assert got.schema.field("c").type == want.schema.field("c").type
    assert "not-a-date" not in want.column("c").to_pylist()


def test_timedelta_overflow_fails_identically_on_both_arms(tmp_path: Path) -> None:
    """A shift past pandas' Timestamp bound raises in the oracle; flag-on raises
    the same exception type (native raises, the slice reroutes, oracle raises)."""
    source = pa.table({"c": pa.array(["2262-04-01", "2024-01-01"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", path, [_ds_column({"min_days": 3000, "max_days": 3000})])
    with pytest.raises(Exception) as off_exc:
        _run(config, path, flag=False)
    with pytest.raises(Exception) as on_exc:
        _run(config, path, flag=True)
    assert type(on_exc.value) is type(off_exc.value)


# ── Unified-slice ExecutionResult boundary (flag-off vs flag-on) ─────


@pytest.mark.parametrize("label, values", _SHAPES, ids=[s[0] for s in _SHAPES])
def test_unified_slice_execution_result_byte_identical(
    tmp_path: Path, label: str, values: list[Any]
) -> None:
    source = pa.table({"c": pa.array(values, type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", path, [_ds_column()])
    off, on = _run(config, path, flag=False), _run(config, path, flag=True)
    ot, nt = off.outputs["t"], on.outputs["t"]
    assert ot.schema.field("c").type == nt.schema.field("c").type
    assert ot.column("c").to_pylist() == nt.column("c").to_pylist()
    assert ot.schema.equals(nt.schema, check_metadata=True)
    if native_companion_status().ok:
        assert QUALITY_METRICS_KEY in on.quality_metrics
        nodes = on.quality_metrics[QUALITY_METRICS_KEY]["nodes"]
        assert nodes
        for ev in nodes.values():
            assert ev["operator"] == "native_date_shift"
            assert ev["executed"] is True
            assert ev["compiled_kernel_executed"] is True
    else:
        assert QUALITY_METRICS_KEY not in on.quality_metrics


def test_unified_slice_parquet_round_trip(tmp_path: Path) -> None:
    source = pa.table(
        {"c": pa.array(["2024-02-29", "2023-02-28", None, "2024-12-31"], type=pa.string())}
    )
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", path, [_ds_column({"min_days": -10, "max_days": 10})])
    off, on = _run(config, path, flag=False), _run(config, path, flag=True)
    pq.write_table(off.outputs["t"], tmp_path / "off.parquet")
    pq.write_table(on.outputs["t"], tmp_path / "on.parquet")
    off_back, on_back = (
        pq.read_table(tmp_path / "off.parquet"),
        pq.read_table(tmp_path / "on.parquet"),
    )
    assert off_back.schema.equals(on_back.schema, check_metadata=True)
    assert off_back.column("c").to_pylist() == on_back.column("c").to_pylist()


def test_mixed_table_with_hash_and_passthrough(tmp_path: Path) -> None:
    source = pa.table(
        {
            "c": pa.array(["2024-02-29", None, "2023-02-28"], type=pa.string()),
            "h": pa.array(["a", "b", "c"], type=pa.string()),
            "p": pa.array([1, 2, 3], type=pa.int64()),
        }
    )
    path = write_read_only_fixture(tmp_path, source, "ds")
    columns = [
        _ds_column(),
        {"name": "h", "strategy": "hash", "namespace": "hns"},
        {"name": "p", "strategy": "passthrough"},
    ]
    config = build_config(tmp_path, "t", path, columns)
    off, on = _run(config, path, flag=False), _run(config, path, flag=True)
    assert off.outputs["t"].equals(on.outputs["t"])
    assert off.outputs["t"].schema.equals(on.outputs["t"].schema, check_metadata=True)
    if native_companion_status().ok:
        assert QUALITY_METRICS_KEY in on.quality_metrics


# ── Companion / index-kernel admission ───────────────────────────────


def test_index_kernel_absent_declines_to_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent INDEX kernel declines at admission (never a load failure inside
    the coordinator, which would surface as an invariant error), so the job runs
    on the oracle and matches flag-off."""
    monkeypatch.setattr(
        admission,
        "native_kernel_availability",
        lambda: KernelAvailability(crypto=True, index=False, raw_hex=True),
    )
    source = pa.table({"c": pa.array(["2024-01-01", None, "2023-06-30"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", path, [_ds_column()])
    off, on = _run(config, path, flag=False), _run(config, path, flag=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert off.outputs["t"].equals(on.outputs["t"])


def test_date_shift_requires_the_index_kernel() -> None:
    assert admission._OPERATOR_REQUIRED_KERNEL[admission.DATE_SHIFT_OPERATOR_ID] == "index"
    assert admission.DATE_SHIFT_OPERATOR_ID in admission._COMPANION_DEPENDENT_OPERATOR_IDS


# ── Relaxed diagnostic-obligation gate is scoped ─────────────────────


def _compiled(config: dict[str, Any], source: pa.Table) -> Any:
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version=ENGINE_VERSION)
    return inputs, compile_physical_plan(inputs)


def _with_binding(plan: Any, **changes: Any) -> Any:
    table = plan.tables[0]
    nodes = tuple(
        dataclasses.replace(n, execution=dataclasses.replace(n.execution, **changes))
        for n in table.nodes
    )
    return dataclasses.replace(plan, tables=(dataclasses.replace(table, nodes=nodes),))


def _admit(inputs: Any, plan: Any, source: pa.Table) -> Any:
    return admission.resident_contract_admission(
        plan,
        table="t",
        source=source,
        plan=inputs.plan,
        registry=inputs.registry,
        graph=inputs.graph,
    )


@_NEEDS_COMPANION
def test_diagnostic_gate_admits_only_date_shift_format_error(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["2024-01-01"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ds")
    inputs, plan = _compiled(build_config(tmp_path, "t", path, [_ds_column()]), source)
    (node,) = plan.tables[0].nodes
    assert node.execution.diagnostic_obligations == ("reduce_row_error:format_error",)
    assert _admit(inputs, plan, source) is not None
    extra = _with_binding(
        plan,
        diagnostic_obligations=("reduce_row_error:format_error", "reduce_warning:x"),
    )
    assert _admit(inputs, extra, source) is None
    other_trigger = _with_binding(plan, diagnostic_obligations=("reduce_row_error:mask_error",))
    assert _admit(inputs, other_trigger, source) is None
    prepass = _with_binding(plan, required_prepasses=("format_detect",))
    assert _admit(inputs, prepass, source) is None


@_NEEDS_COMPANION
def test_diagnostic_gate_still_declines_other_operators(tmp_path: Path) -> None:
    source = pa.table({"h": pa.array(["a"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(
        tmp_path, "t", path, [{"name": "h", "strategy": "hash", "namespace": "hns"}]
    )
    inputs, plan = _compiled(config, source)
    assert _admit(inputs, plan, source) is not None
    carrying = _with_binding(plan, diagnostic_obligations=("reduce_row_error:format_error",))
    assert _admit(inputs, carrying, source) is None


# ── Route / admission declines ───────────────────────────────────────


@pytest.mark.parametrize(
    "column, reason",
    [
        (_ds_column(date_format=None), "date_shift_requires_date_format:c"),
        (_ds_column({"date_format": ""}, date_format=None), "date_shift_requires_date_format:c"),
        (
            _ds_column({"date_format": "mixed"}, date_format=None),
            "date_shift_special_date_format:c",
        ),
        (
            _ds_column({"date_format": "ISO8601"}, date_format=None),
            "date_shift_special_date_format:c",
        ),
        (_ds_column(namespace=None), "date_shift_requires_namespace:c"),
        (_ds_column({"group_by": "pid"}), "date_shift_group_by_not_native:c"),
        (_ds_column(date_format="%Y-%m-%d%z"), "date_shift_timezone_directive:c"),
        (_ds_column(date_format="%Y-%m-%d %Z"), "date_shift_timezone_directive:c"),
        (_ds_column({"min_days": 1.5}), "date_shift_min_days_not_int:c"),
        (_ds_column({"max_days": "30"}), "date_shift_max_days_not_int:c"),
        (_ds_column({"min_days": True}), "date_shift_min_days_not_int:c"),
        (_ds_column({"max_days": 106_752}), "date_shift_max_days_out_of_range:c"),
        (_ds_column({"min_days": -106_752}), "date_shift_min_days_out_of_range:c"),
    ],
)
def test_ineligible_config_declines_on_native_route(
    tmp_path: Path, column: dict[str, Any], reason: str
) -> None:
    config = build_config(tmp_path, "t", tmp_path / "x.parquet", [column])
    result = native_route_eligibility(config, table="t")
    assert not result.accepted
    assert reason in result.rejections, result.rejections


def test_eligible_config_accepted_on_native_route(tmp_path: Path) -> None:
    for column in (
        _ds_column(),
        _ds_column({"min_days": -106_751, "max_days": 106_751}),
        _ds_column(date_format="%Y-%m-%d%%z"),  # escaped literal, not a tz directive
        _ds_column({"group_by": ""}),  # falsy group_by: the oracle ignores it
    ):
        config = build_config(tmp_path, "t", tmp_path / "x.parquet", [column])
        result = native_route_eligibility(config, table="t")
        assert result.accepted, result.rejections


def test_no_date_format_leaves_node_unbound(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["2024-01-01"], type=pa.string())})
    write_read_only_fixture(tmp_path, source, "x")
    config = build_config(tmp_path, "t", tmp_path / "x.parquet", [_ds_column(date_format=None)])
    _, plan = _compiled(config, source)
    nodes = [n for t in plan.tables for n in t.nodes if n.strategy == "date_shift"]
    assert nodes and all(n.execution is None for n in nodes)


def test_chunked_route_declines_date_shift(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["2024-01-01", "2024-02-02"], type=pa.string())})
    write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", tmp_path / "ds.parquet", [_ds_column()])
    profile = first_chunk_profile(source, table="t", engine_version=ENGINE_VERSION)
    preflight = plan_native_route(
        config, profile, table="t", engine_version=ENGINE_VERSION, first_schema=source.schema
    )
    assert preflight.evidence.native_admitted is False
    assert "date_shift_not_native_chunked_route:c" in (preflight.evidence.reroute_reason or "")


@pytest.mark.parametrize(
    "label, source, column",
    [
        (
            "int_source",
            pa.table({"c": pa.array([20240101, 20231231], type=pa.int64())}),
            _ds_column(date_format="%Y%m%d"),
        ),
        (
            "no_format",
            pa.table({"c": pa.array(["2024-01-01", "2023-06-30"], type=pa.string())}),
            _ds_column(date_format=None),
        ),
        (
            "tz_format",
            pa.table({"c": pa.array(["2024-01-01+0000", None], type=pa.string())}),
            _ds_column(date_format="%Y-%m-%d%z"),
        ),
    ],
)
def test_ineligible_job_takes_oracle_on_both_arms(
    tmp_path: Path, label: str, source: pa.Table, column: dict[str, Any]
) -> None:
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", path, [column])
    off, on = _run(config, path, flag=False), _run(config, path, flag=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert off.outputs["t"].column("c").to_pylist() == on.outputs["t"].column("c").to_pylist()
    assert off.outputs["t"].schema.field("c").type == on.outputs["t"].schema.field("c").type


def test_quarantine_configured_job_takes_oracle(tmp_path: Path) -> None:
    """A job WITH quarantine never takes the native lane (job-level decline);
    the oracle quarantines the format_error row."""
    source = pa.table({"c": pa.array(_ERR_VALUES, type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ds")
    config = build_config(tmp_path, "t", path, [_ds_column()])
    config["quarantine"] = {
        "enabled": True,
        "triggers": ["format_error"],
        "output_path": str(tmp_path / "q.jsonl"),
    }
    off, on = _run(config, path, flag=False), _run(config, path, flag=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert off.outputs["t"].equals(on.outputs["t"])
    assert on.outputs["t"].num_rows == len(_ERR_VALUES) - 1


# ── Runtime fail-closed guards at dispatch ───────────────────────────


def _binding(**overrides: Any) -> ExecutionBinding:
    base: dict[str, Any] = dict(
        operator_id="native_date_shift",
        operator_reason="test",
        resolved_config=(),
        input_schema=pa.schema([pa.field("c", pa.string())]),
        output_schema=pa.schema([pa.field("c", pa.string())]),
        determinism_family="source_keyed_hmac",
        determinism_version=1,
        key_binding=KeyBinding(key_source="mask_key", namespace="ns"),
        diagnostic_obligations=("reduce_row_error:format_error",),
        required_prepasses=(),
        batch_estimate=None,
        date_shift_date_format=_FMT,
        date_shift_min_days=-365,
        date_shift_max_days=365,
    )
    base.update(overrides)
    return ExecutionBinding(**base)


def test_run_operator_requires_target_column() -> None:
    ctx = SimpleNamespace(mask_key=_MASK_KEY, native_threads=None)
    with pytest.raises(AssertionError, match="no target column"):
        run_operator(
            pa.array(["2024-01-01"], type=pa.string()),
            binding=_binding(),
            ctx=ctx,  # type: ignore[arg-type]
            evidence=OperatorCallEvidence(planned_operator="native_date_shift"),
            index_kernel=object(),  # type: ignore[arg-type]
        )


def test_date_shift_binding_needs_kernel_not_pool() -> None:
    binding = _binding()
    assert binding.needs_index_kernel is True
    assert binding.pool_binding is None
    assert _binding(date_shift_date_format=None).needs_index_kernel is False


@_NEEDS_COMPANION
def test_run_operator_returns_batch_local_row_errors() -> None:
    ctx = SimpleNamespace(mask_key=_MASK_KEY, native_threads=None)
    out, errors = run_operator(
        pa.array(["2024-01-01", "bad", None], type=pa.string()),
        binding=_binding(),
        ctx=ctx,  # type: ignore[arg-type]
        evidence=OperatorCallEvidence(planned_operator="native_date_shift"),
        index_kernel=load_compiled_index_kernel(),
        column="c",
    )
    assert out.to_pylist()[1:] == ["bad", None]
    assert [(e.column, e.row_index, e.trigger, e.reason) for e in errors] == [
        ("c", 1, "format_error", FORMAT_ERROR_REASON)
    ]
