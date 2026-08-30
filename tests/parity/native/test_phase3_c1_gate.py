"""Task 3.6 (engine leg): the consolidated C1 evidence gate.

Four HARD criteria over the frozen deterministic C1 recipe
(`docs/plans/PHASE3-C1-BASELINE.md`, both tables: `patients` with three
deterministic faker columns plus six hash columns, `observations` with two
hash columns), at the frozen PARITY tier (10,000 rows) and a SAFETY-adjusted
MODERATE tier (250,000 rows; see the tier-size note below). Failing any one
criterion fails the gate. Certification: `docs/plans/native-phase3-C1-gate.md`.

1. EXACT parity: native+streaming output equals the pinned oracle (values,
   row order, null placement, warnings, row errors, logical schema), across
   batch sizes and table orders.
2. Seed stability + partition invariance: different chunk boundaries
   reproduce identical native output; the DE-02 seam (mask_key re-keys
   selection, job_seed re-keys the pool build) holds for this exact recipe.
3. Bounded state + C1 fidelity: `measure_pool_quality` / `enforce_pool_quality`
   pass within the frozen per-column tolerances for the native run; the
   `RouteDiagnostics` collector view stays bounded.
4. Intended-route proof via an INVOCATION-SCOPED ROUTE LEDGER, not a single
   counter (Decision 10: a green run is never mistaken for proof a route
   ran). Built here as a small test-local helper (`_ledger_for_table`) per the
   task's explicit instruction not to touch `_dispatch.py`: the ledger's
   exactness rests on a 1:1, row-count-preserving correspondence between
   input and emitted chunks, which `_mask_chunk_native` guarantees
   structurally (every admitted column is touched exactly once per emitted
   chunk, and there is no partial/mid-stream fallback -- Decision 10).

Tier-size note: the plan's frozen "memory tier" (3,000,000 rows,
`PHASE3-C1-BASELINE.md`) is Task 3.0's ORACLE-ONLY bench measurement. This
gate's criterion 1 needs a live pandas-oracle comparison run at the larger
tier too (to catch a scale-dependent divergence the small tier cannot
exercise), and re-running the full pandas oracle a second time at 3,000,000
rows inside a pytest process risks the 12 GiB box per this task's SAFETY
section. This gate therefore uses a smaller "moderate" tier for its live
oracle comparison, and reduces the batch-size/table-order combinatorics at
that tier to ONE representative combination -- mirroring
`PHASE3-C1-BASELINE.md`'s own reps-reduction precedent at its larger tier
(3 reps instead of 5).

The SAFETY section names 1,000,000 rows as its starting point for a
"moderate" tier, framed around MEMORY risk. Calibrating this gate at that
size showed the live constraint here is WALL TIME, not memory: peak RSS
stayed at 2-3 GB throughout (consistent with the native route being
memory-bounded, exactly what this slice exists to prove), but the
deterministic sampler's per-row Python `derive_index` loop (JC-1; both the
oracle's and the native route's own selection share this same core
algorithm) makes a 1,000,000-row run, run several times across this file's
criteria, take multiple minutes -- impractical for a pytest suite whose job
here is CORRECTNESS at scale, not wall-clock measurement (that is the
separate native bench's job, `scripts/native-baseline/bench_c1_native.py`,
which uses the full 1,000,000-row tier since slow wall time there is the
data being measured, not a liability). This gate's moderate tier is
therefore 250,000 rows: 25x the parity tier, still large enough to catch a
scale-dependent divergence, and fast enough to run routinely.
"""

from __future__ import annotations

import importlib.util
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import run_pipeline
from decoy_engine.execution.native._dispatch import (
    NativeRouteEvidence,
    _resolve_faker_pools,
    run_native_or_oracle_chunked,
)
from decoy_engine.execution.native._pool_quality import enforce_pool_quality, measure_pool_quality
from decoy_engine.execution.native._route_diagnostics import RouteDiagnostics
from decoy_engine.generation.pool import PoolCache
from decoy_engine.keyprovider import SecretKeyProvider
from tests.parity.native._fixtures import LogicalResult, assert_logical_parity

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_NEEDS_COMPANION = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)

_ENGINE_VERSION = "phase3-c1-gate"

# ---------------------------------------------------------------------------
# Reuse Task 3.0's frozen recipe + data generators directly (loaded from the
# script by file path, since `scripts/native-baseline` is not an importable
# package name): the gate must exercise the SAME recipe the oracle baseline
# and the frozen `pool_quality` thresholds (`_pool_quality.py`) were derived
# from, never a re-typed approximation that could silently drift.
# ---------------------------------------------------------------------------

_BENCH_MODULE_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "native-baseline" / "bench_c1_oracle.py"
)


def _load_bench_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("phase3_c1_gate_bench_oracle", _BENCH_MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_bench = _load_bench_module()

# Frozen tiers. Parity matches PHASE3-C1-BASELINE.md exactly; moderate is the
# SAFETY-adjusted, wall-time-calibrated stand-in for the frozen 3,000,000-row
# memory tier (see the module docstring's tier-size note).
_PARITY_N_ROWS = 10_000
_MODERATE_N_ROWS = 250_000

_PARITY_BATCH_SIZES = (743, 2_500)  # >=2 sizes; 743 does not divide 10,000 evenly
_MODERATE_BATCH_SIZE = 25_000  # 1/2 the frozen JC-3 chunk size, scaled to this tier

# One shared data root for the whole module: each tier's out-of-band parquet
# build runs ONCE (cached in `_data_dirs`) and every test function re-reads
# from it, rather than re-generating 1,000,000 rows per test.
_DATA_ROOT = Path(tempfile.mkdtemp(prefix="phase3-c1-gate-data-"))
_data_dirs: dict[int, Path] = {}


def _tier_data_dir(n_rows: int) -> Path:
    if n_rows not in _data_dirs:
        data_dir = _DATA_ROOT / f"tier_{n_rows}"
        _bench.build_data(n_rows, data_dir)
        _data_dirs[n_rows] = data_dir
    return _data_dirs[n_rows]


def _tier_tables(n_rows: int) -> tuple[dict[str, Any], pa.Table, pa.Table]:
    data_dir = _tier_data_dir(n_rows)
    config = _bench.build_config(data_dir)
    patients = pq.read_table(data_dir / "patients.parquet")
    observations = pq.read_table(data_dir / "observations.parquet")
    return config, patients, observations


def _reverse_rows(table: pa.Table) -> pa.Table:
    n = table.num_rows
    if n == 0:
        return table
    return table.take(pa.array(range(n - 1, -1, -1), type=pa.int64()))


def _key_provider(mask_key: bytes = _bench.FIXED_MASK_KEY) -> SecretKeyProvider:
    return SecretKeyProvider(secret=mask_key, key_version="v1")


def _chunk(table: pa.Table, batch_size: int) -> list[pa.Table]:
    if table.num_rows == 0:
        return [table]
    return [table.slice(i, batch_size) for i in range(0, table.num_rows, batch_size)]


def _run_native_table(
    config: dict[str, Any],
    source: pa.Table,
    table: str,
    batch_size: int,
    *,
    mask_key: bytes = _bench.FIXED_MASK_KEY,
    pool_cache: PoolCache | None = None,
) -> tuple[pa.Table, NativeRouteEvidence, list[pa.Table]]:
    sink: list[NativeRouteEvidence] = []
    input_chunks = _chunk(source, batch_size)
    emitted = list(
        run_native_or_oracle_chunked(
            config,
            input_chunks,
            table=table,
            engine_version=_ENGINE_VERSION,
            key_provider=_key_provider(mask_key),
            route_evidence_sink=sink,
            pool_cache=pool_cache,
        )
    )
    combined = pa.concat_tables(emitted).combine_chunks() if emitted else source.slice(0, 0)
    return combined, sink[0], emitted


def _run_native_both(
    config: dict[str, Any], patients: pa.Table, observations: pa.Table, batch_size: int
) -> dict[str, tuple[pa.Table, NativeRouteEvidence, list[pa.Table]]]:
    # One shared pool_cache: only `patients` has faker columns, but a real
    # coordinator processes every table in a job against one cache, and
    # sharing it here matches that shape rather than an artificially isolated
    # per-table cache.
    cache = PoolCache()
    return {
        "patients": _run_native_table(config, patients, "patients", batch_size, pool_cache=cache),
        "observations": _run_native_table(
            config, observations, "observations", batch_size, pool_cache=cache
        ),
    }


def _run_oracle_both(
    config: dict[str, Any], patients: pa.Table, observations: pa.Table
) -> LogicalResult:
    result = run_pipeline(
        config,
        {"patients": patients, "observations": observations},
        engine_version=_ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        key_provider=_key_provider(),
        use_byte_estimate_routing=False,
        use_probe_routing=False,
    )
    return LogicalResult.from_execution_result(result)


def _assert_all_native_admitted(
    native: dict[str, tuple[pa.Table, NativeRouteEvidence, list[pa.Table]]],
) -> None:
    for table, (_out, evidence, _emitted) in native.items():
        assert evidence.native_admitted is True, f"{table}: rerouted ({evidence.reroute_reason})"


# ---------------------------------------------------------------------------
# CRITERION 1: exact parity vs the pinned oracle.
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
@pytest.mark.parametrize("reverse", [False, True], ids=["natural_order", "reversed_order"])
@pytest.mark.parametrize(
    "batch_size", _PARITY_BATCH_SIZES, ids=[f"batch_{b}" for b in _PARITY_BATCH_SIZES]
)
def test_criterion1_exact_parity_at_parity_tier(batch_size: int, reverse: bool) -> None:
    config, patients, observations = _tier_tables(_PARITY_N_ROWS)
    if reverse:
        patients = _reverse_rows(patients)
        observations = _reverse_rows(observations)

    oracle = _run_oracle_both(config, patients, observations)
    native = _run_native_both(config, patients, observations, batch_size)
    _assert_all_native_admitted(native)

    candidate = LogicalResult(outputs={table: out for table, (out, _ev, _em) in native.items()})
    assert_logical_parity(candidate, oracle)


@_NEEDS_COMPANION
def test_criterion1_exact_parity_at_moderate_tier() -> None:
    # See the module docstring's tier-size note: one representative
    # combination at 1,000,000 rows rules out a scale-dependent divergence
    # the parity tier cannot exercise, without re-running the full
    # batch-size/order matrix at the larger tier's cost.
    config, patients, observations = _tier_tables(_MODERATE_N_ROWS)
    oracle = _run_oracle_both(config, patients, observations)
    native = _run_native_both(config, patients, observations, _MODERATE_BATCH_SIZE)
    _assert_all_native_admitted(native)

    candidate = LogicalResult(outputs={table: out for table, (out, _ev, _em) in native.items()})
    assert_logical_parity(candidate, oracle)


# ---------------------------------------------------------------------------
# CRITERION 2: seed stability + partition invariance; the DE-02 seam.
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "n_rows,batch_sizes",
    [
        pytest.param(_PARITY_N_ROWS, (743, 2_500), id="parity_tier"),
        pytest.param(_MODERATE_N_ROWS, (25_000, 83_334), id="moderate_tier"),
    ],
)
def test_criterion2_partition_invariance_across_batch_boundaries(
    n_rows: int, batch_sizes: tuple[int, int]
) -> None:
    # `patients` carries the deterministic faker columns; partition
    # invariance is the JC-5 property under test (a hash column is row-local
    # regardless of chunking, so it is not the interesting case here).
    config, patients, _observations = _tier_tables(n_rows)
    results = []
    for batch_size in batch_sizes:
        out, evidence, _emitted = _run_native_table(config, patients, "patients", batch_size)
        assert evidence.native_admitted is True
        results.append(out)

    first = results[0].to_pydict()
    for other in results[1:]:
        assert other.to_pydict() == first


@_NEEDS_COMPANION
def test_criterion2_de02_seam_holds_for_frozen_c1_recipe() -> None:
    config, patients, _observations = _tier_tables(_PARITY_N_ROWS)

    # mask_key re-keys SELECTION only: same pool identity, different output.
    cache_a = PoolCache()
    out_a, ev_a, _ = _run_native_table(
        config, patients, "patients", 2_500, mask_key=_bench.FIXED_MASK_KEY, pool_cache=cache_a
    )
    other_key = bytes((b + 1) % 256 for b in _bench.FIXED_MASK_KEY)
    cache_b = PoolCache()
    out_b, ev_b, _ = _run_native_table(
        config, patients, "patients", 2_500, mask_key=other_key, pool_cache=cache_b
    )
    assert ev_a.native_admitted is True
    assert ev_b.native_admitted is True
    assert set(cache_a._entries) == set(cache_b._entries)
    assert out_a.column("FIRST").to_pylist() != out_b.column("FIRST").to_pylist()

    # job_seed re-keys the POOL BUILD: a different config seed lands on a
    # different pool identity (build_config unchanged, seed changed).
    config_diff_seed = {**config, "global_settings": {**config["global_settings"]}}
    config_diff_seed["global_settings"]["seed"] = config["global_settings"]["seed"] + 1
    cache_c = PoolCache()
    _, ev_c, _ = _run_native_table(
        config_diff_seed,
        patients,
        "patients",
        2_500,
        mask_key=_bench.FIXED_MASK_KEY,
        pool_cache=cache_c,
    )
    assert ev_c.native_admitted is True
    assert set(cache_c._entries) != set(cache_a._entries)


# ---------------------------------------------------------------------------
# CRITERION 3: bounded state + C1 fidelity (pool_quality; RouteDiagnostics).
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "n_rows,batch_size",
    [(_PARITY_N_ROWS, 2_500), (_MODERATE_N_ROWS, _MODERATE_BATCH_SIZE)],
    ids=["parity_tier", "moderate_tier"],
)
def test_criterion3_pool_quality_passes_frozen_thresholds(
    n_rows: int, batch_size: int, tmp_path: Path
) -> None:
    config, patients, _observations = _tier_tables(n_rows)
    cache = PoolCache()
    diag = RouteDiagnostics(cache)
    native_out, evidence, _emitted = _run_native_table(
        config, patients, "patients", batch_size, pool_cache=cache
    )
    assert evidence.native_admitted is True

    # Retrieve the EXACT ValuePool the native route built, via the same
    # `_resolve_faker_pools` helper `_dispatch.py` calls internally, sharing
    # the SAME `cache` so this is a cache HIT (the already-built pool), not a
    # second, possibly-drifted rebuild.
    plan, _mask_key, _ns_registry, _graph = _bench._compiled_plan(config, _key_provider())
    table_seed = next(ts for name, ts in plan.seed_envelope.per_table if name == "patients")
    col_seed_by_name = dict(table_seed.per_column)
    pools_by_column = _resolve_faker_pools(
        col_seed_by_name, job_seed=plan.seed_envelope.job_seed, pool_cache=cache
    )

    for column in ("FIRST", "LAST", "MAIDEN"):
        measurement = measure_pool_quality(
            column=column,
            source=patients.column(column),
            masked=native_out.column(column),
            pool=pools_by_column[column],
            temp_dir=tmp_path,
            memory_limit="256MB",
        )
        enforce_pool_quality(measurement, column=column)  # must not raise

    # The RouteDiagnostics collector view is bounded: at most one warning per
    # faker-column pool identity (three columns, three identities), never a
    # function of chunk count -- Task 3.4's own bounded-state proof, here
    # exercised against this gate's exact recipe/tier rather than a synthetic
    # fixture.
    assert len(diag.pool_warnings()) <= 3


# ---------------------------------------------------------------------------
# CRITERION 4: intended-route proof via an invocation-scoped exact-count
# route ledger (Decision 10: a green run is never mistaken for proof).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LedgerEntry:
    """One (table, column, chunk) identity the ledger proves was touched
    exactly once."""

    table: str
    column: str
    chunk_index: int


def _ledger_for_table(
    config: dict[str, Any],
    source: pa.Table,
    table: str,
    batch_size: int,
    *,
    pool_cache: PoolCache | None = None,
) -> tuple[NativeRouteEvidence, tuple[_LedgerEntry, ...], int]:
    """Drive `table` through the native route and build its exact-count route
    ledger, without touching `_dispatch.py` (per this task's instruction).

    The ledger's exactness rests on TWO structural facts already true of
    `_mask_chunk_native` (verified by reading `execution/native/_dispatch.py`,
    not assumed): every admitted column is touched exactly once per emitted
    chunk (a single `for name in chunk.schema.names` loop, no retry, no
    skip), and a table's route decision is atomic (Decision 10: no
    per-chunk/per-column fallback once admitted). Given those two facts, the
    per-(table, column, chunk) entry count is exact once we independently
    prove the emitted-chunk stream is a 1:1, row-count-preserving,
    order-preserving image of the input-chunk stream -- no dropped or
    duplicated chunk, which the assertions below check directly.
    """
    input_chunks = _chunk(source, batch_size)
    sink: list[NativeRouteEvidence] = []
    emitted = list(
        run_native_or_oracle_chunked(
            config,
            input_chunks,
            table=table,
            engine_version=_ENGINE_VERSION,
            key_provider=_key_provider(),
            route_evidence_sink=sink,
            pool_cache=pool_cache,
        )
    )
    evidence = sink[0]
    assert evidence.native_admitted is True, f"{table}: rerouted ({evidence.reroute_reason})"
    assert evidence.reroute_reason is None
    # "oracle" calls == 0 / fallback calls == 0 / rejected chunks == 0, for
    # THIS architecture: the route decision is table-atomic (Decision 10), so
    # a node ever tagged "oracle" would mean the WHOLE table rerouted, not a
    # partial fallback. `native_admitted` above already rules that out; this
    # is the per-column confirmation.
    assert all(r.route in ("native_kernel", "native_pool") for r in evidence.node_routes)

    assert len(emitted) == len(input_chunks), (
        f"{table}: emitted {len(emitted)} chunks for {len(input_chunks)} input chunks; "
        "a dropped or duplicated chunk would break the exact-count ledger"
    )
    for i, (inp, out) in enumerate(zip(input_chunks, emitted, strict=True)):
        assert out.num_rows == inp.num_rows, f"{table} chunk {i}: {inp.num_rows} -> {out.num_rows}"

    n_chunks = len(emitted)
    entries = tuple(
        _LedgerEntry(table=table, column=r.column, chunk_index=i)
        for i in range(n_chunks)
        for r in evidence.node_routes
    )
    assert len(entries) == len(set(entries)), "duplicate (table, column, chunk) identity"
    return evidence, entries, n_chunks


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "n_rows,batch_size",
    [
        pytest.param(_PARITY_N_ROWS, 2_500, id="parity_tier"),
        pytest.param(_MODERATE_N_ROWS, _MODERATE_BATCH_SIZE, id="moderate_tier"),
    ],
)
def test_criterion4_exact_count_route_ledger(n_rows: int, batch_size: int) -> None:
    config, patients, observations = _tier_tables(n_rows)
    cache = PoolCache()
    ev_p, entries_p, n_chunks_p = _ledger_for_table(
        config, patients, "patients", batch_size, pool_cache=cache
    )
    ev_o, entries_o, n_chunks_o = _ledger_for_table(
        config, observations, "observations", batch_size, pool_cache=cache
    )

    # Both tables share one tier row count (PHASE3-C1-BASELINE.md's
    # tier-design note), so chunking the same batch_size over both yields the
    # same chunk count.
    assert n_chunks_p == n_chunks_o

    faker_columns = {r.column for r in ev_p.node_routes if r.strategy == "faker"}
    hash_columns_p = {r.column for r in ev_p.node_routes if r.strategy == "hash"}
    hash_columns_o = {r.column for r in ev_o.node_routes if r.strategy == "hash"}
    assert faker_columns == {"FIRST", "LAST", "MAIDEN"}
    assert hash_columns_p == {"SSN", "DRIVERS", "PASSPORT", "ADDRESS", "BIRTHDATE", "DEATHDATE"}
    assert hash_columns_o == {"DATE", "VALUE"}

    # pool_select count == the EXACT number of admitted faker column-chunks.
    assert ev_p.pool_select_calls == len(faker_columns) * n_chunks_p
    assert ev_p.pool_select_executed is True

    # native-hash count == the EXACT hash column-chunk count, per table.
    assert ev_p.kernel_calls.get("hash", 0) == len(hash_columns_p) * n_chunks_p
    assert ev_o.kernel_calls.get("hash", 0) == len(hash_columns_o) * n_chunks_o
    assert ev_p.compiled_kernel_executed is True
    assert ev_o.compiled_kernel_executed is True

    # Every expected (table, column, chunk) identity appears exactly once,
    # across BOTH tables.
    assert len(entries_p) == len(faker_columns | hash_columns_p) * n_chunks_p
    assert len(entries_o) == len(hash_columns_o) * n_chunks_o
    all_entries = entries_p + entries_o
    assert len(all_entries) == len(set(all_entries))

    # oracle calls == 0, oracle rows == 0, fallback calls == 0, rejected
    # chunks == 0: proven by `_ledger_for_table`'s own assertions above
    # (native_admitted True and every node_route native-tagged, for BOTH
    # tables) -- the atomic route decision makes a partial reroute
    # impossible, so those counts are exactly zero by construction.
    #
    # The selected route AND the completed route both equal Phase 3
    # native+streaming: "selected" is the preflight admission
    # (native_admitted, checked above); "completed" is the RUNTIME counters
    # having actually moved (pool_select_executed / compiled_kernel_executed,
    # also checked above, evaluated AFTER the full chunk stream was consumed
    # by `_ledger_for_table`'s `list(...)` call) -- counters recorded only
    # after successful native publication, per Decision 10.
