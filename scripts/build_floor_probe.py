"""Isolate the out-of-core relation-build memory floor from RLIMIT and
allocator-fragmentation confounds, feeding `_memory_estimate.py`'s
`_BUILD_FLOOR_BYTES_PER_ROW` / `_BUILD_FLOOR_BASE_BYTES` calibration.

The prior calibration bracketed its single 20M-row anchor with a DIFFERENT
RLIMIT_DATA at the failing tier (3000 MiB) than the passing tier (5000 MiB),
so the observed floor was partly an rlimit/fragmentation artifact, not a
clean `memory_limit` floor. This probe holds RLIMIT_DATA FIXED well above
every `memory_limit` tier under test (default 8000 MiB), so a failure here
is attributable to DuckDB's own `memory_limit` accounting alone.

Isolates `build_parent_key_relation` (the last-write-wins GROUP BY dedup
`_relation.py`'s module comment documents as the non-spillable floor) rather
than the whole mask+join route: the calibration constant this probe feeds
prices ONLY that phase, and running the narrower path keeps each probe fast
enough to sweep multiple row counts and `memory_limit` tiers by hand.

One row count and one `memory_limit` per process invocation (like
`fk_memory_probe.py`'s per-tier subprocess convention): RLIMIT_DATA cannot be
lowered once raised for a live process, so a fresh process is the only way to
get a clean rlimit per data point.

Usage (binary-search a few `--memory-limit-mib` tiers per row count):

    .venv/bin/python scripts/build_floor_probe.py --rows 5000000 \\
        --memory-limit-mib 512 --json

Every run this script produces during a calibration sweep belongs in the
vault test-results ledger (UTC + commit + rlimit + memory_limit + result),
per the sprint's calibration methodology -- this script only measures one
data point; it does not record the sweep itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Ensure repo root on sys.path when run as a script (matches fk_memory_probe.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
from scripts.fk_memory_probe import _apply_mem_cap, _is_memory_failure, _peak_rss_mb  # noqa: E402

from decoy_engine.execution.out_of_core._relation import build_parent_key_relation  # noqa: E402
from decoy_engine.execution.out_of_core._source import LazySource  # noqa: E402
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed  # noqa: E402
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge  # noqa: E402

_SEED = b"\x00" * 8
_MIB = 1024 * 1024
_WRITE_BATCH_ROWS = 1_000_000


def _probe_plan() -> Any:
    """A minimal plan naming one hash-strategy parent key column -- the only
    plan surface `build_parent_key_relation` reads. Typed `Any` (matching
    `test_out_of_core_runner_phase_caps.py`'s own probe-plan helper): a real
    `Plan` carries far more than `seed_envelope`, so a `SimpleNamespace`
    stand-in structurally satisfies the one attribute this call path reads
    without claiming the full protocol."""
    from types import SimpleNamespace

    col = ColumnSeed(
        namespace="ns_parent",
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("parent", TableSeed(per_column=(("id", col),), per_group=())),),
        )
    )


def _probe_edge() -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="parent",
        parent_columns=("id",),
        child_table="child",
        child_columns=("parent_id",),
        namespace="ns_parent",
        orphan_policy=OrphanPolicy.PRESERVE,
    )


def _write_parent_parquet(path: Path, rows: int) -> None:
    """Stream an `rows`-row single-int64-column parquet in bounded batches --
    the fixture build itself must not hold the whole column resident, or a
    build-time failure could be misattributed to the fixture rather than the
    relation-build phase under test."""
    schema = pa.schema([pa.field("id", pa.int64())])
    writer = pq.ParquetWriter(path, schema)
    try:
        written = 0
        while written < rows:
            n = min(_WRITE_BATCH_ROWS, rows - written)
            arr = pa.array(range(written, written + n), type=pa.int64())
            writer.write_batch(pa.record_batch([arr], schema=schema))
            written += n
    finally:
        writer.close()


def run_probe(rows: int, memory_limit_mib: int) -> dict:
    """One (rows, memory_limit) data point: builds the parent relation under
    that DuckDB cap and classifies the outcome. RLIMIT_DATA is assumed
    already fixed by the caller (`main`'s `--rlimit-mib`, applied once before
    any allocation-heavy work)."""
    with tempfile.TemporaryDirectory(prefix="decoy-build-floor-") as tmp:
        tmp_path = Path(tmp)
        parent_path = tmp_path / "parent.parquet"
        _write_parent_parquet(parent_path, rows)
        source = LazySource(path=parent_path)
        t0 = time.perf_counter()
        result = "pass"
        error: str | None = None
        try:
            build_parent_key_relation(
                plan=_probe_plan(),
                parent=source,
                edge=_probe_edge(),
                temp_dir=tmp_path / "relation",
                memory_limit=f"{memory_limit_mib}MB",
                mask_key=_SEED,
            )
        except BaseException as exc:
            if _is_memory_failure(exc):
                result = "fail"
                error = f"{type(exc).__name__}: {exc}"
            else:
                raise
        elapsed_s = time.perf_counter() - t0
    return {
        "rows": rows,
        "memory_limit_mib": memory_limit_mib,
        "result": result,
        "error": error,
        "elapsed_s": round(elapsed_s, 2),
        "peak_rss_mb": round(_peak_rss_mb(), 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", type=int, required=True, help="parent row count for this data point")
    ap.add_argument(
        "--memory-limit-mib", type=int, required=True, help="DuckDB memory_limit under test"
    )
    ap.add_argument(
        "--rlimit-mib",
        type=int,
        default=8000,
        help=(
            "fixed RLIMIT_DATA ceiling for this process, set well above every "
            "memory_limit tier under test so a failure is attributable to "
            "memory_limit alone, never the rlimit (the prior calibration's "
            "confound)"
        ),
    )
    ap.add_argument("--json", action="store_true", help="emit one JSON line")
    args = ap.parse_args()

    # Applied before any allocation-heavy work (parquet build, DuckDB
    # connection) so the whole process, not just the relation build, runs
    # under the fixed ceiling -- matching what a real job would experience.
    _apply_mem_cap(args.rlimit_mib, "data")
    record = run_probe(args.rows, args.memory_limit_mib)
    record["rlimit_mib"] = args.rlimit_mib
    if args.json:
        print(json.dumps(record))
    else:
        print(record)


if __name__ == "__main__":
    main()
