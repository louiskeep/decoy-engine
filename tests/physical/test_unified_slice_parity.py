"""Task 4.5 D9: the end-to-end unified-slice differential harness.

Drives REAL `run_pipeline` twice per case -- flag off, flag on -- each arm
freshly re-reading the identical Parquet fixture bytes and constructing a
fresh key provider (no state carried between arms). Reuses the 4.4 shadow
corpus's fixed mixed-strategy schema (`tests/physical/test_shadow_corpus.
_FIXED_COLUMNS`) ONLY as a case generator, per the plan -- the assertions
here are new and specific to the unified-slice lane's return contract.

Asserts, per D9:
  (a) the D7 completed-execution evidence is present flag-on / absent
      flag-off;
  (b) `outputs` are cell-identical;
  (c) `warnings` / `table_kinds` / `row_errors` are equal;
  (d) `quality_metrics` is equal EXACTLY once the one named D7 leaf is
      removed, with that leaf itself checked separately in both arms.
`timings` / `boundary_conversion_ms` are explicitly NOT compared (D9 allows
them to differ).

A hash-bearing case is included per the plan's "mixed-four-strategy and a
hash-heavy table" requirement; it needs the optional compiled
`decoy-engine-native` companion to actually EXECUTE through the unified
lane (the same environmental dependency `tests/physical/test_shadow_corpus.
py`'s own hash cases already carry) -- absent it, the admission predicate's
own companion preflight correctly declines and the case still passes as a
pure parity check (both arms take the legacy route and agree), just without
exercising the D7 leaf. The dedicated `test_hash_case_stamps_positive_
kernel_evidence_when_companion_available` below is the one that specifically
needs the companion and is skipped without it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import _pandas_adapter, run_pipeline
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.keyprovider import SecretKeyProvider
from tests.physical._shadow_helpers import build_config, write_read_only_fixture
from tests.physical.test_shadow_corpus import _FIXED_COLUMNS, _build_fixed_source

ENGINE_VERSION = "unified-slice-parity-test"
_MASK_KEY = bytes(range(32))


def _key_provider() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


def _run_both(
    tmp_path: Path, table_name: str, source: pa.Table, columns: list[dict[str, Any]]
) -> tuple[ExecutionResult, ExecutionResult]:
    """Each arm freshly re-reads the fixture Parquet bytes and builds a
    fresh key provider -- no state (a table object, a provider instance) is
    shared between arms."""
    path = write_read_only_fixture(tmp_path, source, "fixture")
    config = build_config(tmp_path, table_name, path, columns)

    off = run_pipeline(
        config,
        {table_name: pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=False,
    )
    on = run_pipeline(
        config,
        {table_name: pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=True,
    )
    return off, on


def _assert_outputs_cell_identical(off: ExecutionResult, on: ExecutionResult) -> None:
    assert set(off.outputs) == set(on.outputs)
    for table in off.outputs:
        off_table, on_table = off.outputs[table], on.outputs[table]
        assert off_table.column_names == on_table.column_names
        assert off_table.num_rows == on_table.num_rows
        for name in off_table.column_names:
            assert off_table.schema.field(name).type == on_table.schema.field(name).type
            assert off_table.column(name).to_pylist() == on_table.column(name).to_pylist()
        # D9 (Codex final-gate BLOCKER): the legacy route builds its output via
        # `Table.from_pandas` (`_pandas_adapter.py:325`), which attaches
        # pandas' own schema metadata (the `b"pandas"` key); a metadata-free
        # table off this lane would silently diverge from what a schema-
        # consuming caller (the platform) sees. Schema equality with
        # `check_metadata=True` catches both a missing key and a byte
        # mismatch, not just a present/absent check.
        assert off_table.schema.equals(on_table.schema, check_metadata=True)


def _assert_quality_metrics_parity(off: ExecutionResult, on: ExecutionResult) -> dict[str, Any]:
    assert QUALITY_METRICS_KEY not in off.quality_metrics
    assert QUALITY_METRICS_KEY in on.quality_metrics
    on_leaf = on.quality_metrics[QUALITY_METRICS_KEY]
    on_without_leaf = {k: v for k, v in on.quality_metrics.items() if k != QUALITY_METRICS_KEY}
    assert on_without_leaf == off.quality_metrics
    assert on_leaf["activated"] is True
    assert on_leaf["nodes"], "activation evidence must cover at least one node"
    for evidence in on_leaf["nodes"].values():
        assert evidence["executed"] is True
    return on_leaf


def _assert_full_parity(off: ExecutionResult, on: ExecutionResult) -> dict[str, Any]:
    _assert_outputs_cell_identical(off, on)
    assert tuple(off.warnings) == tuple(on.warnings)
    assert off.table_kinds == on.table_kinds
    assert tuple(off.row_errors) == tuple(on.row_errors)
    return _assert_quality_metrics_parity(off, on)


def test_passthrough_alone_admits_and_matches(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    off, on = _run_both(tmp_path, "t", source, [{"name": "c", "strategy": "passthrough"}])
    _assert_full_parity(off, on)


def test_redact_and_truncate_mixed_admits_and_matches(tmp_path: Path) -> None:
    source = pa.table(
        {
            "p": pa.array(["x", "y", "z"], type=pa.string()),
            "r": pa.array(["s1", "s2", "s3"], type=pa.string()),
            "tr": pa.array(["abcdef", "ghijkl", "mnopqr"], type=pa.string()),
        }
    )
    columns = [
        {"name": "p", "strategy": "passthrough"},
        {"name": "r", "strategy": "redact"},
        {"name": "tr", "strategy": "truncate", "provider_config": {"length": 3}},
    ]
    off, on = _run_both(tmp_path, "t", source, columns)
    _assert_full_parity(off, on)


def test_null_density_passthrough_admits_and_matches(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", None, "b", None, "c"], type=pa.string())})
    off, on = _run_both(tmp_path, "t", source, [{"name": "c", "strategy": "passthrough"}])
    _assert_full_parity(off, on)


def test_empty_admitted_table_matches(tmp_path: Path) -> None:
    """A zero-row resident table across all four strategies -- the shadow
    coordinator's own C6 zero-row-batch contract (`_shadow_coordinator.
    _batches`) must still reassemble a schema-identical empty output."""
    source = pa.table(
        {
            "p": pa.array([], type=pa.string()),
            "r": pa.array([], type=pa.string()),
            "tr": pa.array([], type=pa.string()),
            "h": pa.array([], type=pa.string()),
        }
    )
    columns = [
        {"name": "p", "strategy": "passthrough"},
        {"name": "r", "strategy": "redact"},
        {"name": "tr", "strategy": "truncate", "provider_config": {"length": 2}},
        {"name": "h", "strategy": "hash", "namespace": "n"},
    ]
    off, on = _run_both(tmp_path, "t", source, columns)
    _assert_outputs_cell_identical(off, on)
    assert on.outputs["t"].num_rows == 0


def test_all_null_admitted_table_matches(tmp_path: Path) -> None:
    """An all-null (non-empty) resident column per strategy; hash is exempt
    (an all-null hash column has no non-null value to derive, but is
    otherwise a normal null-free-int/string check -- covered separately by
    the null-density case above for string, and `reject_null_bearing_int`
    for int)."""
    source = pa.table(
        {
            "p": pa.array([None, None, None], type=pa.string()),
            "r": pa.array([None, None, None], type=pa.string()),
            "tr": pa.array([None, None, None], type=pa.string()),
        }
    )
    columns = [
        {"name": "p", "strategy": "passthrough"},
        {"name": "r", "strategy": "redact"},
        {"name": "tr", "strategy": "truncate", "provider_config": {"length": 2}},
    ]
    off, on = _run_both(tmp_path, "t", source, columns)
    _assert_full_parity(off, on)


def test_mixed_four_strategy_fixed_schema_case_generator(tmp_path: Path) -> None:
    """Reuses the 4.4 corpus's fixed mixed-strategy schema as a case
    generator ONLY -- assertions are this module's own. Requires the
    compiled native companion for the hash column to actually execute
    through the unified lane; when the companion is absent, admission
    correctly declines (both arms take the legacy route) and the outputs
    still match, just without a D7 leaf to assert."""
    source = _build_fixed_source(23)
    path = write_read_only_fixture(tmp_path, source, "fixed_schema")
    config = build_config(tmp_path, "w", path, _FIXED_COLUMNS)

    off = run_pipeline(
        config,
        {"w": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=False,
    )
    on = run_pipeline(
        config,
        {"w": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=True,
    )
    _assert_outputs_cell_identical(off, on)
    assert tuple(off.warnings) == tuple(on.warnings)
    assert tuple(off.row_errors) == tuple(on.row_errors)
    if QUALITY_METRICS_KEY in on.quality_metrics:
        _assert_quality_metrics_parity(off, on)


@pytest.mark.skipif(
    not native_companion_status().ok, reason="compiled decoy-engine-native companion unavailable"
)
def test_hash_case_stamps_positive_kernel_evidence_when_companion_available(
    tmp_path: Path,
) -> None:
    source = pa.table({"c": pa.array(["a@x.com", "b@x.com", "c@x.com"], type=pa.string())})
    columns = [{"name": "c", "strategy": "hash", "namespace": "n"}]
    off, on = _run_both(tmp_path, "t", source, columns)
    leaf = _assert_full_parity(off, on)
    (evidence,) = leaf["nodes"].values()
    assert evidence["operator"] == "native_keyed_hash"
    assert evidence["compiled_kernel_executed"] is True


# ---------------------------------------------------------------------------
# Non-vacuity: poison the legacy pandas adapter on the flag-on admitted path.
# ---------------------------------------------------------------------------


def test_flag_on_admitted_run_never_calls_the_legacy_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D7/D9: poisons `PandasExecutionAdapter.run` so any accidental legacy
    execution on an ADMITTED flag-on path fails loudly instead of silently
    passing parity against itself."""
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "fixture")
    config = build_config(tmp_path, "t", path, [{"name": "c", "strategy": "passthrough"}])

    def _poisoned_run(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "the legacy PandasExecutionAdapter.run must not be called on an "
            "admitted unified-slice run"
        )

    monkeypatch.setattr(_pandas_adapter.PandasExecutionAdapter, "run", _poisoned_run)

    on = run_pipeline(
        config,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=True,
    )
    assert QUALITY_METRICS_KEY in on.quality_metrics
    assert on.outputs["t"].column("c").to_pylist() == ["a", "b", "c"]


def test_flag_off_run_still_calls_the_legacy_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror of the poison test above: with the flag off, the SAME
    admissible config must still take the legacy route -- proving the
    poison itself is a meaningful signal, not a fixture that never
    reaches the adapter either way."""
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "fixture")
    config = build_config(tmp_path, "t", path, [{"name": "c", "strategy": "passthrough"}])

    calls: list[int] = []
    real_run = _pandas_adapter.PandasExecutionAdapter.run

    def _counting_run(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real_run(self, *args, **kwargs)

    monkeypatch.setattr(_pandas_adapter.PandasExecutionAdapter, "run", _counting_run)

    run_pipeline(
        config,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=False,
    )
    assert calls == [1]


# ---------------------------------------------------------------------------
# Failure parity: a config both routes reject IDENTICALLY. The unified
# lane's own admission declines this shape (a null-bearing int under hash),
# so the flag-on call takes the SAME legacy code path the flag-off call
# does -- proving type/code/message parity by construction, not by
# reproducing the message by hand.
# ---------------------------------------------------------------------------


def test_null_bearing_int_under_hash_fails_identically_both_flags(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import ExecutionError

    source = pa.table({"c": pa.array([1, None, 3], type=pa.int64())})
    path = write_read_only_fixture(tmp_path, source, "fixture")
    config = build_config(
        tmp_path, "t", path, [{"name": "c", "strategy": "hash", "namespace": "n"}]
    )

    def _call(flag: bool) -> ExecutionError:
        with pytest.raises(ExecutionError) as excinfo:
            run_pipeline(
                config,
                {"t": pq.read_table(path)},
                engine_version=ENGINE_VERSION,
                key_provider=_key_provider(),
                unified_slice_enabled=flag,
            )
        return excinfo.value

    off_exc = _call(False)
    on_exc = _call(True)
    assert off_exc.code == on_exc.code == "null_bearing_int_unsupported"
    assert str(off_exc) == str(on_exc)


# ---------------------------------------------------------------------------
# Substrate gate: the lane is parity-guaranteed against the PANDAS full-frame
# route only. A polars-substrate job must decline to the unchanged old route
# (it runs a different legacy adapter with its own provenance telemetry).
# ---------------------------------------------------------------------------


def test_polars_substrate_declines_to_legacy_route(tmp_path: Path) -> None:
    pytest.importorskip("polars")
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "fixture")
    config = build_config(tmp_path, "t", path, [{"name": "c", "strategy": "passthrough"}])

    on = run_pipeline(
        config,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=True,
        substrate="polars",
    )
    # No activation leaf => the lane declined and the polars legacy route ran.
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert on.outputs["t"].column("c").to_pylist() == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Batch boundary: a table larger than the coordinator's 50k internal batch
# but below the 100k auto-chunk threshold both ACTIVATES the lane and spans
# multiple coordinator batches, exercising per-batch reassembly. Non-hash so
# it needs no compiled companion and always activates.
# ---------------------------------------------------------------------------


def test_multi_batch_table_admits_and_matches(tmp_path: Path) -> None:
    n = 80_000  # > ShadowContext.batch_size_rows (50k), < auto_chunk_threshold (100k)
    source = pa.table(
        {
            "p": pa.array([f"p{i % 97}" for i in range(n)], type=pa.string()),
            "r": pa.array([f"s{i % 13}" for i in range(n)], type=pa.string()),
            "tr": pa.array([f"abcdef{i % 7}" for i in range(n)], type=pa.string()),
        }
    )
    columns = [
        {"name": "p", "strategy": "passthrough"},
        {"name": "r", "strategy": "redact"},
        {"name": "tr", "strategy": "truncate", "provider_config": {"length": 3}},
    ]
    off, on = _run_both(tmp_path, "t", source, columns)
    _assert_full_parity(off, on)
    assert on.outputs["t"].num_rows == n


# ---------------------------------------------------------------------------
# Domain boundary (CHANGE 3, Codex determination): the fixed per-strategy
# admitted-type matrix is {string,int64,bool} for passthrough, {string} for
# redact/truncate, {string, null-free int64} for hash. A type outside a
# strategy's own set must FALL BACK to the legacy route (both arms still
# agree, since both take the same route), never admit and mis-execute.
# ---------------------------------------------------------------------------


def test_out_of_domain_dtype_columns_decline_to_the_legacy_route(tmp_path: Path) -> None:
    """The narrowed replacement for the old (incorrect) "non-string dtypes
    admit" assumption: every column below is outside its strategy's fixed
    matrix, so the whole table must decline -- both arms take the legacy
    route and trivially agree, with no D7 activation evidence at all."""
    source = pa.table(
        {
            "rf": pa.array([1.5, 2.5, 3.5], type=pa.float64()),
            "rb": pa.array([True, False, True], type=pa.bool_()),
            "pf": pa.array([1.1, 2.2, 3.3], type=pa.float64()),
            "pts": pa.array([1, 2, 3], type=pa.timestamp("us")),
        }
    )
    columns = [
        {"name": "rf", "strategy": "redact"},
        {"name": "rb", "strategy": "redact"},
        {"name": "pf", "strategy": "passthrough"},
        {"name": "pts", "strategy": "passthrough"},
    ]
    off, on = _run_both(tmp_path, "t", source, columns)
    _assert_outputs_cell_identical(off, on)
    assert QUALITY_METRICS_KEY not in on.quality_metrics


def test_passthrough_int64_and_bool_admit_and_match(tmp_path: Path) -> None:
    """The positive control: int64/bool ARE in passthrough's admitted
    matrix (unlike float64/timestamp above) and must activate + match."""
    source = pa.table(
        {
            "pi": pa.array([1, 2, 3], type=pa.int64()),
            "pb": pa.array([True, False, True], type=pa.bool_()),
        }
    )
    columns = [
        {"name": "pi", "strategy": "passthrough"},
        {"name": "pb", "strategy": "passthrough"},
    ]
    off, on = _run_both(tmp_path, "t", source, columns)
    _assert_full_parity(off, on)


@pytest.mark.skipif(
    not native_companion_status().ok, reason="compiled decoy-engine-native companion unavailable"
)
def test_hash_int64_admits_and_matches(tmp_path: Path) -> None:
    """hash's matrix admits null-free int64 too, not just string."""
    source = pa.table({"c": pa.array([10, 20, 30], type=pa.int64())})
    columns = [{"name": "c", "strategy": "hash", "namespace": "n"}]
    off, on = _run_both(tmp_path, "t", source, columns)
    leaf = _assert_full_parity(off, on)
    (evidence,) = leaf["nodes"].values()
    assert evidence["compiled_kernel_executed"] is True


# ---------------------------------------------------------------------------
# CHANGE 2 proof: source-shaped output assembly preserves provenance a
# generic coordinator-output round-trip would lose. A `StringDtype`-backed
# resident column (its Arrow table carries `numpy_type: "string"` pandas
# metadata, not the plain-`object` default) is left untouched for
# passthrough, so its metadata survives by construction; the OLD design
# (round-tripping the coordinator's own metadata-free `pa.table(...)`
# output) could not have reproduced this.
# ---------------------------------------------------------------------------


def test_string_dtype_column_matches_exact_metadata_while_activated(tmp_path: Path) -> None:
    frame = pd.DataFrame({"c": pd.array(["a", "b", "c"], dtype="string")})
    source = pa.Table.from_pandas(frame, preserve_index=False)
    off, on = _run_both(tmp_path, "t", source, [{"name": "c", "strategy": "passthrough"}])
    leaf = _assert_full_parity(off, on)
    assert leaf["activated"] is True
    meta = json.loads(on.outputs["t"].schema.metadata[b"pandas"])
    (col_meta,) = [c for c in meta["columns"] if c["name"] == "c"]
    assert col_meta["numpy_type"] == "string"


# ---------------------------------------------------------------------------
# Exact Parquet read-back parity for an admitted case (D9): a real
# write-then-read round trip on BOTH arms' outputs, not just the in-memory
# `ExecutionResult.outputs` table objects.
# ---------------------------------------------------------------------------


def test_admitted_case_matches_after_a_parquet_round_trip(tmp_path: Path) -> None:
    source = pa.table(
        {
            "p": pa.array(["x", "y", "z"], type=pa.string()),
            "r": pa.array(["s1", "s2", "s3"], type=pa.string()),
        }
    )
    columns = [
        {"name": "p", "strategy": "passthrough"},
        {"name": "r", "strategy": "redact"},
    ]
    off, on = _run_both(tmp_path, "t", source, columns)
    _assert_full_parity(off, on)

    off_path = tmp_path / "off_roundtrip.parquet"
    on_path = tmp_path / "on_roundtrip.parquet"
    pq.write_table(off.outputs["t"], off_path)
    pq.write_table(on.outputs["t"], on_path)
    off_back = pq.read_table(off_path)
    on_back = pq.read_table(on_path)
    assert off_back.schema.equals(on_back.schema, check_metadata=True)
    assert set(off_back.column_names) == set(on_back.column_names)
    for name in off_back.column_names:
        assert off_back.column(name).to_pylist() == on_back.column(name).to_pylist()
