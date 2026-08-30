"""Task 2.7: the frozen Phase 2 gate (correctness + route-proof + keyed-kernel).

The HARD criteria named in `docs/plans/2026-08-28-part1-phase2-detail-DRAFT.md`
("Task 2.7") and `docs/plans/PHASE2-BASELINE.md` (targets 1-3): exact seeded
logical parity against the pinned pandas oracle across batch sizes and row
orders, proof every admitted node routed to a native kernel (the route TAG, not
job success -- Decision 10), and proof the COMPILED keyed-hash kernel executed.
Wall-time and peak-RSS (targets 4-5) are measured separately by
`scripts/native-baseline/bench_worker_native.py` under the fresh-process
external-RSS harness; this file is the correctness/route/kernel half only.

The workload mirrors W2 (`scripts/native-baseline/bench_worker.py`): 3 keyed
hash columns, 3 passthrough, 2 redact, 2 truncate, PLUS one legacy
`from_end: true` truncate column (carry-forward #3 -- the route-proof harness
must exercise it). Run at 3 batch sizes and 2 row orders (natural + a fixed
permutation), each compared against its OWN pinned-oracle run on the same row
order (a value-keyed strategy's output does not depend on row order OR
chunking, so this proves both dimensions independently).
"""

from __future__ import annotations

import importlib.util
import tempfile
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution.native import _dispatch
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._dispatch import (
    NativeRouteEvidence,
    run_native_or_oracle_chunked,
)
from decoy_engine.keyprovider import SecretKeyProvider
from tests.parity.native._fixtures import (
    DEFAULT_ALLOWED_PHYSICAL_DIFFS,
    LogicalResult,
    PhysicalDiff,
    assert_logical_parity,
)

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_NEEDS_COMPANION = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)

_MASK_KEY = bytes.fromhex("a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90")
_ENGINE_VERSION = "phase2-gate"


def _key_provider() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


# ---------------------------------------------------------------------------
# W2-shaped workload: 3 hash, 3 passthrough, 2 redact, 2 truncate + 1 legacy
# from_end truncate column (11 columns total).
# ---------------------------------------------------------------------------

_COLUMNS = [
    {"name": "h_email", "strategy": "hash", "namespace": "ns_email"},
    {"name": "h_token", "strategy": "hash", "namespace": "ns_token"},
    {"name": "h_uid", "strategy": "hash", "namespace": "ns_uid"},
    {"name": "pt_amount", "strategy": "passthrough"},
    {"name": "pt_flag", "strategy": "passthrough"},
    {"name": "pt_ts", "strategy": "passthrough"},
    {"name": "rd_ssn", "strategy": "redact"},
    {"name": "rd_notes", "strategy": "redact"},
    {"name": "tr_phone", "strategy": "truncate", "provider_config": {"length": 3, "keep": "head"}},
    {"name": "tr_card", "strategy": "truncate", "provider_config": {"length": 4, "keep": "tail"}},
    {
        "name": "tr_legacy",
        "strategy": "truncate",
        "provider_config": {"length": 4, "from_end": True},
    },
]


def _build_source(n_rows: int, *, reverse: bool = False) -> pa.Table:
    # Every column gets an EXPLICIT Arrow type, including the string ones: a
    # zero-row Python list carries no inferable type, so a naive `pa.table`
    # over `[]` would come back `null`-typed even though a real streaming
    # source (Parquet-backed) always carries its declared schema, empty batch
    # or not. Pinning the type here keeps the empty-table gate case realistic.
    idx = list(range(n_rows))
    if reverse:
        idx = list(reversed(idx))
    _str = pa.string()
    table = pa.table(
        {
            "h_email": pa.array([f"user{i}@example.com" for i in idx], type=_str),
            "h_token": pa.array([f"tok_{i}" for i in idx], type=_str),
            "h_uid": pa.array([100_000_000 + i for i in idx], type=pa.int64()),
            "pt_amount": pa.array([(i * 13) % 1_000_000 for i in idx], type=pa.int64()),
            "pt_flag": pa.array([i % 2 == 0 for i in idx], type=pa.bool_()),
            "pt_ts": pa.array(
                [1_600_000_000_000_000 + i * 1_000_000 for i in idx],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "rd_ssn": pa.array([f"5{i % 900:03d}-11-2222" for i in idx], type=_str),
            "rd_notes": pa.array([f"note-{i}" for i in idx], type=_str),
            "tr_phone": pa.array([f"512{i % 9000:04d}" for i in idx], type=_str),
            "tr_card": pa.array([f"4000{i % 9999:04d}" for i in idx], type=_str),
            "tr_legacy": pa.array([f"CARD{i:04d}XYZ" for i in idx], type=_str),
        }
    )
    return table


_SOURCE_DIR = tempfile.mkdtemp(prefix="phase2-gate-src-")


def _build_config(
    source: pa.Table, *, key: str, columns: list[dict[str, Any]] | None = None
) -> dict:
    path = f"{_SOURCE_DIR}/{key}.parquet"
    pq.write_table(source, path)
    raw = {
        "version": 1,
        "global_settings": {"seed": 20260828, "post_validation": False},
        "sources": {"w2": {"type": "file", "format": "parquet", "path": path}},
        "targets": {"w2": {"type": "file", "format": "parquet", "path": f"{path}.out"}},
        "tables": [{"name": "w2", "columns": columns if columns is not None else _COLUMNS}],
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
            table="w2",
            engine_version=_ENGINE_VERSION,
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )
    return pa.concat_tables(chunks).combine_chunks(), sink[0]


def _run_oracle(config: dict, source: pa.Table) -> LogicalResult:
    result = run_pipeline(
        config,
        {"w2": source},
        engine_version=_ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        key_provider=_key_provider(),
        use_byte_estimate_routing=False,
        use_probe_routing=False,
    )
    return LogicalResult.from_execution_result(result)


# ---------------------------------------------------------------------------
# Output-schema reconciliation (carry-forward #4): native emits each admitted
# strategy's stable Arrow type across every batch shape; the pandas oracle's
# `pa.Table.from_pandas` round-trip infers a DIFFERENT type for a degenerate
# whole-COLUMN shape (all-null -> null-type; zero-row -> double, pandas'
# empty-column default). Both are pandas inference artifacts, not real value
# divergences (characterized in `tests/native/test_kernels_scalar.py`), so they
# are reconciled here -- but ONLY these two exact type-pairs; anything else
# still fails the comparison.
# ---------------------------------------------------------------------------


def _is_empty_double_normalization(oracle_type: pa.DataType, candidate_type: pa.DataType) -> bool:
    return oracle_type.equals(pa.float64()) and candidate_type.equals(pa.string())


EMPTY_DOUBLE_NORMALIZATION = PhysicalDiff(
    name="empty_double_normalization",
    decision=(
        "_kernels_scalar.py: a zero-row batch's redact/truncate/hash column comes "
        "back from pandas' `pa.Table.from_pandas` as float64 (pandas' empty-column "
        "default); native emits the strategy's stable pa.string() for every batch "
        "shape. Both describe zero values, so the type difference is a pandas "
        "inference artifact, not a value divergence."
    ),
    predicate=_is_empty_double_normalization,
)

_GATE_ALLOWED_DIFFS = (*DEFAULT_ALLOWED_PHYSICAL_DIFFS, EMPTY_DOUBLE_NORMALIZATION)


def _assert_gate_parity(native_table: pa.Table, oracle: LogicalResult) -> None:
    assert oracle.warnings == ()
    assert oracle.row_errors == ()
    candidate = LogicalResult(outputs={"w2": native_table})
    assert_logical_parity(candidate, oracle, allowed_physical_diffs=_GATE_ALLOWED_DIFFS)


# ---------------------------------------------------------------------------
# HARD criterion 1: exact seeded correctness across 3 batch sizes x 2 orders.
# ---------------------------------------------------------------------------

_N_ROWS = 37  # not a multiple of any batch size below: exercises a ragged last chunk
_BATCH_SIZES = (1, 4, 11)
_ORDERS = (False, True)  # natural, then a fixed reversal


@_NEEDS_COMPANION
@pytest.mark.parametrize("reverse", _ORDERS, ids=["natural_order", "reversed_order"])
@pytest.mark.parametrize("batch_size", _BATCH_SIZES, ids=[f"batch_{b}" for b in _BATCH_SIZES])
def test_native_route_exact_parity_vs_oracle(batch_size: int, reverse: bool) -> None:
    source = _build_source(_N_ROWS, reverse=reverse)
    config = _build_config(source, key=f"parity_{batch_size}_{reverse}")
    oracle = _run_oracle(config, source)
    native_table, evidence = _run_native(config, source, batch_size)

    assert evidence.native_admitted is True
    _assert_gate_parity(native_table, oracle)


@_NEEDS_COMPANION
def test_native_route_parity_on_all_null_column() -> None:
    # Degenerate whole-column shape #1: every value null (pandas infers
    # null-type; the NULL_TYPED_NORMALIZATION reconciles it).
    source = _build_source(9)
    source = source.set_column(
        source.schema.get_field_index("rd_notes"),
        "rd_notes",
        pa.array([None] * 9, type=pa.string()),
    )
    config = _build_config(source, key="all_null")
    oracle = _run_oracle(config, source)
    native_table, evidence = _run_native(config, source, batch_size=4)
    assert evidence.native_admitted is True
    _assert_gate_parity(native_table, oracle)


@_NEEDS_COMPANION
def test_native_route_parity_on_empty_table() -> None:
    # Degenerate whole-column shape #2: zero rows (pandas infers double for
    # the redact/truncate/hash columns; EMPTY_DOUBLE_NORMALIZATION reconciles).
    # Feeding ONE zero-row chunk (not zero chunks) exercises the real "empty
    # batch reaches a kernel" case `_kernels_scalar.py` characterizes, rather
    # than the unrelated "no chunks at all" short-circuit.
    source = _build_source(0)
    config = _build_config(source, key="empty_table")
    oracle = _run_oracle(config, source)
    sink: list[NativeRouteEvidence] = []
    chunks = list(
        run_native_or_oracle_chunked(
            config,
            [source],
            table="w2",
            engine_version=_ENGINE_VERSION,
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )
    evidence = sink[0]
    assert evidence.native_admitted is True
    native_table = pa.concat_tables(chunks).combine_chunks()
    _assert_gate_parity(native_table, oracle)


# ---------------------------------------------------------------------------
# HARD criterion 2: route-proof. Job success is never the evidence; the route
# tag is.
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
def test_route_proof_every_node_dispatches_to_native_kernel() -> None:
    source = _build_source(13)
    config = _build_config(source, key="route_proof")
    _, evidence = _run_native(config, source, batch_size=4)

    assert evidence.native_admitted is True
    assert evidence.reroute_reason is None
    routed = {r.column: r.route for r in evidence.node_routes}
    assert set(routed) == {c["name"] for c in _COLUMNS}
    assert all(route == "native_kernel" for route in routed.values())


@_NEEDS_COMPANION
def test_route_proof_oracle_success_is_not_native_proof() -> None:
    # The Decision-10 trap, made explicit: running the SAME config on the
    # pinned oracle succeeds too. That success says nothing about whether the
    # native route ran; only the route tag from the native call does.
    source = _build_source(13)
    config = _build_config(source, key="route_proof_oracle_control")
    oracle = _run_oracle(config, source)
    assert set(oracle.outputs) == {"w2"}  # the oracle succeeded
    # ...which proves nothing about the native route; only this does:
    _, evidence = _run_native(config, source, batch_size=4)
    assert all(r.route == "native_kernel" for r in evidence.node_routes)


# ---------------------------------------------------------------------------
# HARD criterion 3: the COMPILED keyed-hash kernel executed (not the reference).
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
def test_keyed_hash_gate_compiled_kernel_executed() -> None:
    source = _build_source(13)
    config = _build_config(source, key="keyed_gate")
    _, evidence = _run_native(config, source, batch_size=4)

    assert evidence.compiled_kernel_executed is True
    assert evidence.kernel_calls.get("hash", 0) > 0


# ---------------------------------------------------------------------------
# Preflight-only reroute: missing/ABI-incompatible extension never runs
# mid-stream; the whole table falls back, and the fallback output still
# matches the oracle exactly.
# ---------------------------------------------------------------------------


def test_extension_absent_reroutes_whole_table_and_still_matches_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unavailable() -> None:
        raise CryptoExtensionUnavailableError("simulated absence")

    monkeypatch.setattr(_dispatch, "load_compiled_crypto_kernel", _unavailable)

    source = _build_source(13)
    config = _build_config(source, key="ext_absent")
    oracle = _run_oracle(config, source)
    native_table, evidence = _run_native(config, source, batch_size=4)

    assert evidence.native_admitted is False
    assert evidence.reroute_reason == "crypto_extension_unavailable"
    assert all(r.route == "oracle" for r in evidence.node_routes)
    assert evidence.kernel_calls == {}
    assert evidence.compiled_kernel_executed is False
    _assert_gate_parity(native_table, oracle)


# ---------------------------------------------------------------------------
# A non-admitted column keeps the WHOLE table on the oracle.
# ---------------------------------------------------------------------------


def test_mixed_admitted_and_non_admitted_columns_stays_fully_on_oracle() -> None:
    # The T6 end-to-end route-proof: every admitted column here (3 hash, 3
    # passthrough, 2 redact, 3 truncate) has a compiled kernel on its own, but
    # `extra_redact` (text_redact) does not -- so the whole table, including
    # its otherwise-admitted columns, reroutes. Proving that boundary end to
    # end needs all three legs together: every node tagged oracle, the
    # compiled kernel NEVER ran (not merely "the job succeeded" -- Decision
    # 10's trap), and the output still matches the oracle exactly.
    columns = [*_COLUMNS, {"name": "extra_redact", "strategy": "text_redact"}]
    source = _build_source(13).append_column(
        "extra_redact", pa.array([f"contact me at u{i}@x.com" for i in range(13)])
    )
    config = _build_config(source, key="mixed_non_admitted", columns=columns)
    oracle = _run_oracle(config, source)
    native_table, evidence = _run_native(config, source, batch_size=4)

    assert evidence.native_admitted is False
    assert all(r.route == "oracle" for r in evidence.node_routes)
    assert evidence.kernel_calls == {}
    assert evidence.compiled_kernel_executed is False
    _assert_gate_parity(native_table, oracle)
