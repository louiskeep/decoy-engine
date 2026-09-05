"""Acceptance tests 1-4 and 7 (docs/plans/2026-09-05-native-route-wider-types.md
section 7): the native route's widened admission for integer, boolean, and
timestamp columns, driven through the PRODUCTION entry (`run_pipeline` with
`native_route_enabled=True`) exactly like `test_native_route_production_seam.py`
does for slice 1. Test 5 (flat memory) and the two-read benchmark live in
`tests/perf/test_native_route_wider_types_memory.py`; test 8 (mutation bar) is
a separate infra run, not a pytest case; the codec/matrix unit-level tests
live in `tests/unit/execution/test_native_route_preflight_units.py`.

A tz-aware timestamp column trips a PRE-EXISTING gap in the (unrelated)
byte-estimate routing signal (`_mem_estimate.py` has no fixed-width entry
for a tz-aware `datetime64`), so every case using one passes
`use_byte_estimate_routing=False` -- the same workaround
`test_dictionary_utf8_reroutes` already uses for a different unpriced dtype.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution import _pipeline_sources as _psrc
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.profile._readers import LazySource
from tests.parity.native._fixtures import LogicalResult, assert_logical_parity

_ENGINE_VERSION = "native-route-wider-types-test"
_TABLE = "t"


# ---------------------------------------------------------------------------
# Config/source construction helpers
# ---------------------------------------------------------------------------


def _write_source(tmp_path: Path, table: pa.Table, name: str = "src") -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _config(
    tmp_path: Path, columns: list[dict[str, Any]], *, table: pa.Table, name: str = "src"
) -> tuple[dict, Path]:
    source_path = _write_source(tmp_path, table, name)
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": 20260905},
        "sources": {_TABLE: {"type": "file", "format": "parquet", "path": str(source_path)}},
        "targets": {
            _TABLE: {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / f"{name}.out.parquet"),
            }
        },
        "tables": [{"name": _TABLE, "columns": columns}],
    }
    return PipelineConfig.model_validate(raw).model_dump(), source_path


def _pt(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "strategy": "passthrough", **extra}


def _rd(name: str, redact_with: Any = "REDACTED", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "redact",
        "provider_config": {"redact_with": redact_with},
        **extra,
    }


def _tr(name: str, length: int = 3, keep: str = "head", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "truncate",
        "provider_config": {"length": length, "keep": keep},
        **extra,
    }


def _run_native(config: dict[str, Any], source_path: Path, **kwargs: Any):
    sources = {_TABLE: LazySource(path=source_path)}
    kwargs.setdefault("use_byte_estimate_routing", False)
    return run_pipeline(
        config,
        sources,
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
        **kwargs,
    )


def _run_full_frame_oracle(config: dict[str, Any], source_path: Path, **kwargs: Any):
    sources = {_TABLE: LazySource(path=source_path)}
    kwargs.setdefault("use_byte_estimate_routing", False)
    return run_pipeline(
        config,
        sources,
        engine_version=_ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Test 1: the full normative matrix, parity through the production entry
# ---------------------------------------------------------------------------

_INT_TYPES: list[pa.DataType] = [
    pa.int8(),
    pa.int16(),
    pa.int32(),
    pa.int64(),
    pa.uint8(),
    pa.uint16(),
    pa.uint32(),
    pa.uint64(),
]
_TS_TYPES: list[pa.DataType] = [
    pa.timestamp("s"),
    pa.timestamp("ms"),
    pa.timestamp("us"),
    pa.timestamp("ns"),
    pa.timestamp("ms", tz="UTC"),
    pa.timestamp("s", tz="America/New_York"),  # DST-aware
    pa.timestamp("s", tz="+05:30"),  # fixed offset
]

_BOUNDARY_BY_WIDTH: dict[pa.DataType, tuple[int, int]] = {
    pa.int8(): (-128, 127),
    pa.int16(): (-32768, 32767),
    pa.int32(): (-2147483648, 2147483647),
    pa.int64(): (-(2**63), 2**63 - 1),
    pa.uint8(): (0, 255),
    pa.uint16(): (0, 65535),
    pa.uint32(): (0, 4294967295),
    pa.uint64(): (0, 2**64 - 1),
}


def _int_values(arrow_type: pa.DataType, state: str) -> list[Any]:
    lo, hi = _BOUNDARY_BY_WIDTH[arrow_type]
    if state == "no_null":
        return [lo, 0 if lo <= 0 <= hi else hi, hi]
    if state == "partial_null":
        return [lo, None, hi]
    if state == "all_null":
        return [None, None, None]
    return []  # empty


def _bool_values(state: str) -> list[Any]:
    if state == "no_null":
        return [True, False, True]
    if state == "partial_null":
        return [True, None, False]
    if state == "all_null":
        return [None, None, None]
    return []


def _ts_values(state: str) -> list[Any]:
    # Negative epoch + a fractional-precision-bearing value (123456789 in
    # whatever unit the column carries) alongside a mid-range value.
    if state == "no_null":
        return [-86400, 0, 123456789]
    if state == "partial_null":
        return [-86400, None, 123456789]
    if state == "all_null":
        return [None, None, None]
    return []


_STATES = ("no_null", "partial_null", "all_null", "empty")

# (strategy, family) -> per-state expected outcome: "admit", "reroute", or
# "oracle_error" (raises ExecutionError before any native/oracle comparison).
_MATRIX_OUTCOMES: dict[tuple[str, str], dict[str, str]] = {
    ("passthrough", "integer"): {
        "no_null": "admit",
        "partial_null": "reroute",
        "all_null": "reroute",
        "empty": "admit",
    },
    ("passthrough", "boolean"): {
        "no_null": "admit",
        "partial_null": "admit",
        "all_null": "reroute",
        "empty": "admit",
    },
    ("passthrough", "timestamp"): {
        "no_null": "admit",
        "partial_null": "admit",
        "all_null": "admit",
        "empty": "admit",
    },
    ("redact", "integer"): {
        "no_null": "admit",
        "partial_null": "admit",
        "all_null": "reroute",
        "empty": "reroute",
    },
    ("redact", "boolean"): {
        "no_null": "admit",
        "partial_null": "admit",
        "all_null": "reroute",
        "empty": "reroute",
    },
    ("redact", "timestamp"): {
        "no_null": "admit",
        "partial_null": "admit",
        "all_null": "reroute",
        "empty": "reroute",
    },
    ("truncate", "integer"): {
        "no_null": "admit",
        "partial_null": "oracle_error",
        "all_null": "oracle_error",
        "empty": "reroute",
    },
    ("truncate", "boolean"): {
        "no_null": "admit",
        "partial_null": "admit",
        "all_null": "reroute",
        "empty": "reroute",
    },
    ("truncate", "timestamp"): {
        "no_null": "admit",
        "partial_null": "admit",
        "all_null": "reroute",
        "empty": "reroute",
    },
}


def _matrix_cases() -> list[tuple[str, str, pa.DataType, str, str]]:
    cases: list[tuple[str, str, pa.DataType, str, str]] = []
    for (strategy, family), by_state in _MATRIX_OUTCOMES.items():
        types = {"integer": _INT_TYPES, "boolean": [pa.bool_()], "timestamp": _TS_TYPES}[family]
        for arrow_type in types:
            for state, outcome in by_state.items():
                cases.append((strategy, family, arrow_type, state, outcome))
    return cases


def _values_for(family: str, arrow_type: pa.DataType, state: str) -> list[Any]:
    if family == "integer":
        return _int_values(arrow_type, state)
    if family == "boolean":
        return _bool_values(state)
    return _ts_values(state)


def _column_for(strategy: str, name: str) -> dict[str, Any]:
    if strategy == "passthrough":
        return _pt(name)
    if strategy == "redact":
        return _rd(name)
    return _tr(name, length=2)


@pytest.mark.parametrize(
    "strategy,family,arrow_type,state,outcome",
    _matrix_cases(),
    ids=[f"{s}/{f}/{t!s}/{st}" for s, f, t, st, _ in _matrix_cases()],
)
def test_full_matrix_parity_through_production_entry(
    tmp_path: Path, strategy: str, family: str, arrow_type: pa.DataType, state: str, outcome: str
) -> None:
    values = _values_for(family, arrow_type, state)
    table = pa.table({"c": pa.array(values, type=arrow_type)})
    column = _column_for(strategy, "c")
    config, source_path = _config(tmp_path, [column], table=table)

    if outcome == "oracle_error":
        with pytest.raises(ExecutionError) as excinfo:
            _run_native(config, source_path)
        assert excinfo.value.code == "null_bearing_int_unsupported"
        return

    candidate = _run_native(config, source_path)
    assert candidate.native_route is not None
    if outcome == "admit":
        assert candidate.native_route.admitted is True, candidate.native_route.reason
    else:
        assert candidate.native_route.admitted is False
        assert candidate.native_route.reason is not None
        assert candidate.native_route.reason.startswith("native_preflight_reroute:")

    oracle = _run_full_frame_oracle(config, source_path)
    candidate_logical = LogicalResult.from_execution_result(candidate)
    oracle_logical = LogicalResult.from_execution_result(oracle)
    if outcome == "admit":
        # Plan 7.1 requires exact physical-cell parity for an Admit cell: the
        # native lane emits its own output, so the default null-typed
        # normalization must not be allowed to silently absorb an all-null
        # drift. No current Admit cell relies on it.
        assert_logical_parity(candidate_logical, oracle_logical, allowed_physical_diffs=())
    else:
        # A reroute runs the ordinary chunked continuation, whose per-chunk
        # all-null concat is exactly the drift the default normalization
        # covers; keep the default allow-list here.
        assert_logical_parity(candidate_logical, oracle_logical)


# ---------------------------------------------------------------------------
# Test 2: preflight resolves GLOBAL state, not first-batch
# ---------------------------------------------------------------------------


def _late_null_table(n: int, null_row: int, arrow_type: pa.DataType) -> pa.Table:
    values: list[Any] = list(range(n))
    values[null_row] = None
    return pa.table({"c": pa.array(values, type=arrow_type)})


def test_late_null_beyond_profile_sample_and_first_batch_reroutes(tmp_path: Path) -> None:
    """A null at row 60,000 -- past both the 10,000-row profile sample and
    the lane's own 50,000-row native batch -- reroutes exactly like one at
    row 5 would; the preflight's own full pass is what catches it.
    (passthrough/integer: the one cell where a first-batch-only look would
    wrongly conclude no_null/Admit; redact/integer admits at partial_null
    either way, so it cannot distinguish a global scan from a first-batch
    one through the admission outcome alone -- see the redact case below,
    which instead checks the late null still lands correctly in the
    output.)"""
    n = 60_001
    table = _late_null_table(n, 60_000, pa.int64())
    config, source_path = _config(tmp_path, [_pt("c")], table=table)
    candidate = _run_native(config, source_path)
    assert candidate.native_route is not None
    assert candidate.native_route.admitted is False
    assert (
        candidate.native_route.reason
        == "native_preflight_reroute:c:passthrough:integer:partial_null"
    )
    oracle = _run_full_frame_oracle(config, source_path)
    assert_logical_parity(
        LogicalResult.from_execution_result(candidate), LogicalResult.from_execution_result(oracle)
    )


def test_late_null_redact_admits_with_correct_null_position(tmp_path: Path) -> None:
    """redact/integer/partial_null Admits regardless of null position (the
    matrix cell), so this proves the OTHER half of the "global, not first
    batch" claim: a null far past the first batch still lands correctly in
    the native output (parity with the oracle), not silently dropped or
    mis-redacted because some earlier pass only glimpsed the first batch."""
    n = 60_001
    table = _late_null_table(n, 60_000, pa.int64())
    config, source_path = _config(tmp_path, [_rd("c")], table=table)
    candidate = _run_native(config, source_path)
    assert candidate.native_route is not None and candidate.native_route.admitted is True
    oracle = _run_full_frame_oracle(config, source_path)
    assert_logical_parity(
        LogicalResult.from_execution_result(candidate), LogicalResult.from_execution_result(oracle)
    )


def test_late_null_int_truncate_raises_execution_guard_not_compile_error(
    tmp_path: Path,
) -> None:
    """Section 6: on the production Parquet path, a null-bearing integer
    truncate raises the EXISTING execution-time guard whether the null is
    at row 5 or after row 50,000 -- never `PlanCompileError`, because a
    nullable Parquet integer profiles as pandas `float64`."""
    n = 60_001
    table = _late_null_table(n, 60_000, pa.int64())
    config, source_path = _config(tmp_path, [_tr("c", length=2)], table=table)
    with pytest.raises(ExecutionError) as excinfo:
        _run_native(config, source_path)
    assert excinfo.value.code == "null_bearing_int_unsupported"


# ---------------------------------------------------------------------------
# Test 3: partial-null vs all-null are distinguished
# ---------------------------------------------------------------------------


def test_partial_null_and_all_null_resolve_to_different_verdicts(tmp_path: Path) -> None:
    partial = pa.table({"c": pa.array([1, None], type=pa.int64())})
    all_null = pa.table({"c": pa.array([None, None], type=pa.int64())})

    config_p, path_p = _config(tmp_path, [_pt("c")], table=partial, name="partial")
    config_a, path_a = _config(tmp_path, [_pt("c")], table=all_null, name="all_null")

    result_p = _run_native(config_p, path_p)
    result_a = _run_native(config_a, path_a)

    assert (
        result_p.native_route.reason
        == "native_preflight_reroute:c:passthrough:integer:partial_null"
    )
    assert result_a.native_route.reason == "native_preflight_reroute:c:passthrough:integer:all_null"
    assert result_p.native_route.reason != result_a.native_route.reason


# ---------------------------------------------------------------------------
# Test 4: source-snapshot digest catches a mutation between the two reads
# ---------------------------------------------------------------------------


def _rewriting_source(path: Path, calls: dict[str, int], mutated: pa.Table) -> LazySource:
    class _RewritingSource(LazySource):
        def iter_batches(self, batch_rows: int):
            calls["n"] += 1
            if calls["n"] == 2:
                st = os.stat(self.path)
                pq.write_table(mutated, self.path)
                os.utime(self.path, (st.st_atime, st.st_mtime))
            yield from super().iter_batches(batch_rows)

    return _RewritingSource(path=path)


def test_digest_mismatch_aborts_before_commit_streaming_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `LazySource` subclass rewrites the same path (fixed-width, same
    row count, restored mtime) between the two `iter_batches` calls: exactly
    two runtime iterator calls, coded abort, no final artifact, no oracle
    read -- never a silent commit of stale-verdict output."""
    original = pa.table({"c": pa.array([1, 2, 3, 4], type=pa.int64())})
    mutated = pa.table({"c": pa.array([1, 2, 3, 999], type=pa.int64())})
    source_path = _write_source(tmp_path, original)
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {_TABLE: {"type": "file", "format": "parquet", "path": str(source_path)}},
            "targets": {
                _TABLE: {"type": "file", "format": "parquet", "path": str(tmp_path / "out.parquet")}
            },
            "tables": [{"name": _TABLE, "columns": [_pt("c")]}],
        }
    ).model_dump()

    calls = {"n": 0}
    source = _rewriting_source(source_path, calls, mutated)
    sink_dir = tmp_path / "sink"
    sink = ParquetTransactionalSink(sink_dir)

    from decoy_engine.execution import _chunked as _chunked_mod

    oracle_calls = {"n": 0}
    orig_chunked = _chunked_mod.run_mask_pipeline_chunked

    def _spy(*args: Any, **kwargs: Any):
        oracle_calls["n"] += 1
        return orig_chunked(*args, **kwargs)

    monkeypatch.setattr(_chunked_mod, "run_mask_pipeline_chunked", _spy)
    with pytest.raises(ExecutionError) as excinfo:
        run_pipeline(
            config,
            {_TABLE: source},
            engine_version=_ENGINE_VERSION,
            native_route_enabled=True,
            execution_mode="auto",
            sink=sink,
            use_byte_estimate_routing=False,
        )

    assert excinfo.value.code == "native_source_snapshot_digest_mismatch"
    assert calls["n"] == 2, f"expected exactly 2 iter_batches calls, got {calls['n']}"
    assert not (sink_dir / f"{_TABLE}.parquet").exists(), "a staged/final artifact must not survive"
    assert oracle_calls["n"] == 0, "digest mismatch must never fall back to the oracle"


def test_digest_mismatch_aborts_resident_mode(tmp_path: Path) -> None:
    """Same guarantee, `sink=None` (resident) mode: the exception propagates
    and no resident table is ever returned."""
    original = pa.table({"c": pa.array([True, False, True], type=pa.bool_())})
    mutated = pa.table({"c": pa.array([True, False, False], type=pa.bool_())})
    source_path = _write_source(tmp_path, original)
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {_TABLE: {"type": "file", "format": "parquet", "path": str(source_path)}},
            "targets": {
                _TABLE: {"type": "file", "format": "parquet", "path": str(tmp_path / "out.parquet")}
            },
            "tables": [{"name": _TABLE, "columns": [_pt("c")]}],
        }
    ).model_dump()
    calls = {"n": 0}
    source = _rewriting_source(source_path, calls, mutated)
    with pytest.raises(ExecutionError) as excinfo:
        run_pipeline(
            config,
            {_TABLE: source},
            engine_version=_ENGINE_VERSION,
            native_route_enabled=True,
            execution_mode="auto",
            sink=None,
            use_byte_estimate_routing=False,
        )
    assert excinfo.value.code == "native_source_snapshot_digest_mismatch"
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Test 5 (partial): the native lane provably ran without materialization
# on a WIDENED (two-read) admitted job
# ---------------------------------------------------------------------------


def test_widened_admitted_job_ledger_zero_and_two_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = 5_000
    table = pa.table(
        {
            "pt_int": pa.array(list(range(n)), type=pa.int64()),
            "pt_bool": pa.array([i % 2 == 0 for i in range(n)], type=pa.bool_()),
        }
    )
    config, source_path = _config(tmp_path, [_pt("pt_int"), _pt("pt_bool")], table=table)

    def _boom(*args: Any, **kwargs: Any):
        raise AssertionError("resolve_resident_sources was called on the widened native path")

    monkeypatch.setattr(_psrc, "resolve_resident_sources", _boom)
    from decoy_engine.execution import _pipeline as _pipeline_mod

    monkeypatch.setattr(_pipeline_mod._psrc, "resolve_resident_sources", _boom)

    calls = {"n": 0}
    source = LazySource(path=source_path)
    orig_iter_batches = source.iter_batches

    def _counting_iter_batches(batch_rows: int):
        calls["n"] += 1
        return orig_iter_batches(batch_rows)

    object.__setattr__(source, "iter_batches", _counting_iter_batches)

    result = run_pipeline(
        config,
        {_TABLE: source},
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
        use_byte_estimate_routing=False,
    )
    assert result.native_route is not None and result.native_route.admitted is True
    ledger = result.native_route.ledger
    assert ledger is not None
    assert ledger.oracle_calls == 0
    assert ledger.oracle_rows == 0
    assert ledger.fallback_calls == 0
    assert ledger.fallback_rows == 0
    assert ledger.rejected_chunks == 0
    assert ledger.native_attempted == ledger.native_completed
    assert ledger.native_attempted > 0
    assert calls["n"] == 2, f"expected exactly 2 iter_batches calls, got {calls['n']}"
