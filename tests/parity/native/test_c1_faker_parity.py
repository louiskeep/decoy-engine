"""Task 3.1: exact logical parity between the native chunked route and the
pinned pandas oracle for the deterministic C1 faker variant (JC-5).

Workload mirrors the frozen C1 recipe (`docs/plans/PHASE3-C1-BASELINE.md`):
two deterministic `person_first_name` / `person_last_name` faker columns plus
a `hash` column, each with an explicit namespace and pool_size. Exercised
across multiple batch sizes (including one that does not divide the row
count evenly), natural and reversed row order, and source data with repeated
distinct values (collisions are expected and must reproduce identically) and
nulls at the first row, the last row, a run of consecutive rows, and scattered
positions -- including chunks whose Arrow offset is non-zero (every chunk
after the first).
"""

from __future__ import annotations

import tempfile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution.native._dispatch import (
    NativeRouteEvidence,
    run_native_or_oracle_chunked,
)
from decoy_engine.keyprovider import SecretKeyProvider
from tests.parity.native._fixtures import LogicalResult as _LogicalResult
from tests.parity.native._fixtures import assert_logical_parity

_ENGINE_VERSION = "phase3-task3.1-parity"
_MASK_KEY = bytes.fromhex("b1c2d3e4f5061728394a5b6c7d8e9f0a1b2c3d4e5f60718293a4b5c6d7e8f901")


def _key_provider() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


_COLUMNS = [
    {
        "name": "FIRST",
        "strategy": "faker",
        "provider": "person_first_name",
        "deterministic": True,
        "namespace": "first_name_identity",
        "pool_size": 200,
    },
    {
        "name": "LAST",
        "strategy": "faker",
        "provider": "person_last_name",
        "deterministic": True,
        "namespace": "last_name_identity",
        "pool_size": 200,
    },
    {"name": "SSN", "strategy": "hash", "namespace": "ssn_identity"},
]


def _build_source(n_rows: int, *, reverse: bool = False) -> pa.Table:
    idx = list(range(n_rows))
    if reverse:
        idx = list(reversed(idx))
    # A small distinct-source vocabulary so repeated values (and therefore
    # deterministic-reuse collisions) are exercised, not just unique rows.
    n_distinct = max(1, n_rows // 4)

    def _null_positions(total: int) -> set[int]:
        # First row, last row, a run of two consecutive rows in the middle,
        # and every 5th row -- first/last/consecutive/scattered null shapes.
        positions = {0, total - 1} if total else set()
        mid = total // 2
        positions |= {mid, mid + 1} if total > 3 else set()
        positions |= {i for i in range(total) if i % 5 == 0}
        return positions

    nulls = _null_positions(n_rows)
    first = pa.array(
        [None if i in nulls else f"first_src_{i % n_distinct}" for i in idx], type=pa.string()
    )
    last = pa.array(
        [None if i in nulls else f"last_src_{i % n_distinct}" for i in idx], type=pa.string()
    )
    ssn = pa.array(
        [None if i in nulls else f"5{i % 900:03d}-11-2222" for i in idx], type=pa.string()
    )
    return pa.table({"FIRST": first, "LAST": last, "SSN": ssn})


_SOURCE_DIR = tempfile.mkdtemp(prefix="phase3-c1-faker-parity-")
_source_file_counter = 0


def _build_config(source: pa.Table, *, seed: int = 20260830) -> dict:
    # `profile_source` reads the CONFIGURED path for real (row counts, dtype
    # inference), even though the actual masking data is supplied in-memory
    # to `run_pipeline`/`run_native_or_oracle_chunked`; a real file must exist
    # there (mirrors `tests/parity/native/test_phase2_gate.py`).
    global _source_file_counter
    _source_file_counter += 1
    path = f"{_SOURCE_DIR}/c1_{_source_file_counter}.parquet"
    pq.write_table(source, path)
    raw = {
        "version": 1,
        "global_settings": {"seed": seed, "post_validation": False},
        "sources": {"c1": {"type": "file", "format": "parquet", "path": path}},
        "targets": {"c1": {"type": "file", "format": "parquet", "path": f"{path}.out"}},
        "tables": [{"name": "c1", "columns": _COLUMNS}],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _chunk(table: pa.Table, batch_size: int) -> list[pa.Table]:
    return [table.slice(i, batch_size) for i in range(0, table.num_rows, batch_size)]


def _run_native(
    config: dict, source: pa.Table, batch_size: int
) -> tuple[pa.Table, NativeRouteEvidence]:
    sink: list[NativeRouteEvidence] = []
    chunks = list(
        run_native_or_oracle_chunked(
            config,
            _chunk(source, batch_size),
            table="c1",
            engine_version=_ENGINE_VERSION,
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )
    return pa.concat_tables(chunks).combine_chunks(), sink[0]


def _run_oracle(config: dict, source: pa.Table) -> _LogicalResult:
    # Must use the SAME key_provider as `_run_native` below: the shared
    # `tests.parity.native._fixtures.run_oracle` helper does not accept one,
    # and comparing against job_seed-fallback mask_key would compare two
    # DIFFERENT masking jobs, not the same job on two routes.
    result = run_pipeline(
        config,
        {"c1": source},
        engine_version=_ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        key_provider=_key_provider(),
        use_byte_estimate_routing=False,
        use_probe_routing=False,
    )
    return _LogicalResult.from_execution_result(result)


_N_ROWS = 33  # not a multiple of any batch size below: exercises a ragged last chunk
_BATCH_SIZES = (1, 4, 11)
_ORDERS = (False, True)  # natural, then reversed


@pytest.mark.parametrize("reverse", _ORDERS, ids=["natural_order", "reversed_order"])
@pytest.mark.parametrize("batch_size", _BATCH_SIZES, ids=[f"batch_{b}" for b in _BATCH_SIZES])
def test_native_faker_route_exact_parity_vs_oracle(batch_size: int, reverse: bool) -> None:
    source = _build_source(_N_ROWS, reverse=reverse)
    config = _build_config(source)
    oracle = _run_oracle(config, source)
    native_table, evidence = _run_native(config, source, batch_size)

    assert evidence.native_admitted is True
    candidate = _LogicalResult(outputs={"c1": native_table})
    assert_logical_parity(candidate, oracle)


def test_native_faker_route_parity_on_all_null_faker_column() -> None:
    source = _build_source(10)
    source = source.set_column(
        source.schema.get_field_index("FIRST"), "FIRST", pa.array([None] * 10, type=pa.string())
    )
    config = _build_config(source)
    oracle = _run_oracle(config, source)
    native_table, evidence = _run_native(config, source, batch_size=3)
    assert evidence.native_admitted is True
    assert_logical_parity(_LogicalResult(outputs={"c1": native_table}), oracle)


def test_native_faker_route_parity_with_non_zero_offset_single_row_chunks() -> None:
    # Every chunk after the first has a non-zero Arrow offset; batch_size=1
    # maximizes the count of non-zero-offset chunks relative to row count.
    source = _build_source(9)
    config = _build_config(source)
    oracle = _run_oracle(config, source)
    native_table, evidence = _run_native(config, source, batch_size=1)
    assert evidence.native_admitted is True
    assert_logical_parity(_LogicalResult(outputs={"c1": native_table}), oracle)


def test_route_evidence_matches_column_count_and_native_pool_tag() -> None:
    source = _build_source(20)
    config = _build_config(source)
    _, evidence = _run_native(config, source, batch_size=6)

    assert evidence.native_admitted is True
    routed = {r.column: r.route for r in evidence.node_routes}
    assert routed == {"FIRST": "native_pool", "LAST": "native_pool", "SSN": "native_kernel"}
    # 4 chunks (batch_size=6, n=20) x 2 faker columns = 8 pool_select calls.
    assert evidence.pool_select_calls == 8
    assert evidence.pool_select_executed is True
    assert evidence.compiled_kernel_executed is True
    assert evidence.kernel_calls.get("hash") == 4
