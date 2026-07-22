"""Fixed-parent, growing-child memory probe for the OOC-B child-key regression.

`scripts/fk_memory_probe.py --mode out_of_core --rows N` scales the parent,
child, and grandchild tables TOGETHER (one `rows` knob), which the
OOC-B memory-fix implementation plan (Task 1) found can mask a child-only
regression: the relation-build floor scales with PARENT rows
(`_memory_estimate.predict_ooc_build_floor_bytes`), so a symmetric sweep
conflates that floor with the child-side joiner cost this probe isolates.
This script fixes parent cardinality and varies only the child, over a single
`customers -> orders` edge (`run_fk_out_of_core`, the real production
entrypoint -- never a reimplementation of the join), so a resident,
O(child)-scaling structure in the joiner shows up as a growing `peak_rss_mb`
slope across child sizes at FIXED parent size, and a properly spillable one
plateaus.

Backs `tests/perf/test_out_of_core_memory_sentinel.py`'s TQ-0 plateau
sentinels: this is the RED/GREEN measurement for the `StreamFkJoiner`
child-side regression (`_stream_join.py`'s `child_keys` TEMP TABLE, pre-fix)
and its `SpillChildKeys`-backed replacement (post-fix).

Usage (single tier, JSON line for the test harness):

    .venv/bin/python scripts/ooc_child_key_plateau_probe.py \\
        --parent-rows 150000 --child-rows 600000 --payload-width 0 \\
        --memory-limit-mb 32 --json

Run each tier in a FRESH subprocess (matching `fk_memory_probe.py`'s own
isolation pattern) with `ARROW_DEFAULT_MEMORY_POOL=system` and
`MALLOC_ARENA_MAX=2` set in the environment BEFORE the process starts: the
allocator otherwise reserves address space far beyond live data, which would
mask (or falsely trigger) the RSS slope this probe exists to measure -- see
the implementation plan's "CRITICAL harness note".
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from decoy_engine.execution import ParquetTransactionalSink  # noqa: E402
from decoy_engine.execution.out_of_core import run_fk_out_of_core  # noqa: E402
from decoy_engine.execution.out_of_core._source import LazySource  # noqa: E402
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed  # noqa: E402
from decoy_engine.providers_v2 import get_default_registry  # noqa: E402
from decoy_engine.relationships._graph import (  # noqa: E402
    OrphanPolicy,
    RelationshipEdge,
    RelationshipGraph,
)

_NS = "ooc_plateau"
_SEED = (0xC01D).to_bytes(8, "big")
# Chunked generation, matching `write_large_fk_chain`'s discipline: no whole
# table is ever resident during fixture construction either, so the peak this
# probe reports is the RUN's peak, not an artifact of building its input.
_CHUNK_ROWS = 50_000
_FILLER_POOL = 4096


def _peak_rss_mb() -> float:
    """VmHWM (see `fk_memory_probe.py::_peak_rss_mb` for why not `ru_maxrss`:
    it survives execve and over-reports a subprocess spawned by a large
    parent). A high-water mark, so reading it after the work tree is removed
    is still accurate -- cleanup only frees memory, never raises the mark."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _hash_col() -> ColumnSeed:
    return ColumnSeed(
        namespace=_NS,
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _string_pool(rng: np.random.Generator, n: int, width: int = 12) -> np.ndarray:
    alphabet = np.frombuffer(b"abcdefghijklmnopqrstuvwxyz0123456789", dtype="S1")
    idx = rng.integers(0, len(alphabet), size=(n, width))
    return np.array([b"".join(row).decode("ascii") for row in alphabet[idx]], dtype=object)


def _schema(payload_width: int) -> pa.Schema:
    return pa.schema(
        [pa.field("customer_id", pa.string())]
        + [pa.field(f"payload_{i:02d}", pa.string()) for i in range(payload_width)]
    )


def _write_parent(path: Path, parent_rows: int, payload_width: int, seed: int) -> None:
    """Parent keys `p0..p{parent_rows-1}`, written in bounded chunks."""
    rng = np.random.default_rng(seed)
    pool = _string_pool(rng, _FILLER_POOL)
    schema = _schema(payload_width)
    writer = pq.ParquetWriter(str(path), schema)
    try:
        for start in range(0, parent_rows, _CHUNK_ROWS):
            length = min(_CHUNK_ROWS, parent_rows - start)
            idx = np.arange(start, start + length)
            cols: dict = {"customer_id": ("p" + idx.astype("U")).astype(object)}
            for i in range(payload_width):
                take = rng.integers(0, len(pool), size=length)
                cols[f"payload_{i:02d}"] = pool[take]
            writer.write_table(pa.table(cols, schema=schema))
    finally:
        writer.close()


def _write_child(
    path: Path,
    child_rows: int,
    parent_rows: int,
    payload_width: int,
    orphan_frac: float,
    seed: int,
) -> None:
    """Child FK keys computed from the row index alone (`idx % parent_rows`),
    so referential integrity holds without the parent resident and the
    child's own row count is INDEPENDENT of the parent's -- the one property
    `write_large_fk_chain` (which ties both to a single `rows`) does not give
    us. `orphan_every` plants a chunk-boundary-independent orphan fraction,
    mirroring `tests/perf_fixtures/fk_relational.py::_fk_from_idx`.
    """
    rng = np.random.default_rng(seed + 1)
    pool = _string_pool(rng, _FILLER_POOL)
    schema = _schema(payload_width)
    orphan_every = round(1 / orphan_frac) if orphan_frac > 0 else 0
    writer = pq.ParquetWriter(str(path), schema)
    try:
        for start in range(0, child_rows, _CHUNK_ROWS):
            length = min(_CHUNK_ROWS, child_rows - start)
            idx = np.arange(start, start + length)
            keys = ("p" + (idx % parent_rows).astype("U")).astype(object)
            if orphan_every:
                is_orphan = (idx % orphan_every) == 0
                if is_orphan.any():
                    keys = keys.copy()
                    keys[is_orphan] = ("orphan_" + idx[is_orphan].astype("U")).astype(object)
            cols: dict = {"customer_id": keys}
            for i in range(payload_width):
                take = rng.integers(0, len(pool), size=length)
                cols[f"payload_{i:02d}"] = pool[take]
            writer.write_table(pa.table(cols, schema=schema))
    finally:
        writer.close()


def _plan() -> SimpleNamespace:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("customers", TableSeed(per_column=(("customer_id", _hash_col()),), per_group=())),
                ("orders", TableSeed(per_column=(("customer_id", _hash_col()),), per_group=())),
            ),
        )
    )


def _graph(policy: OrphanPolicy) -> RelationshipGraph:
    return RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="customers",
                parent_columns=("customer_id",),
                child_table="orders",
                child_columns=("customer_id",),
                namespace=_NS,
                orphan_policy=policy,
            ),
        ),
        ordering=(),
    )


def run_one(
    parent_rows: int,
    child_rows: int,
    payload_width: int,
    memory_limit_mb: int,
    orphan_frac: float,
    seed: int = 20260722,
) -> dict:
    """Run one (parent_rows, child_rows) tier through the production
    out-of-core route under an EXPLICIT `memory_limit`, and report VmHWM.

    A single edge (`customers -> orders`) is enough to exercise
    `StreamFkJoiner` exactly as the multi-edge production route does; adding
    a grandchild would only add the (already-understood, already-spillable)
    relation-build cost this probe deliberately isolates away from.
    """
    work_root = Path(tempfile.mkdtemp(prefix="decoy-ooc-plateau-"))
    out_rows = 0
    null_out = 0
    try:
        src_dir = work_root / "src"
        src_dir.mkdir()
        parent_path = src_dir / "customers.parquet"
        child_path = src_dir / "orders.parquet"
        _write_parent(parent_path, parent_rows, payload_width, seed)
        _write_child(child_path, child_rows, parent_rows, payload_width, orphan_frac, seed)

        target = work_root / "output" / "published"
        sink = ParquetTransactionalSink(target)
        sources = {
            "customers": LazySource(path=parent_path),
            "orders": LazySource(path=child_path),
        }
        run_fk_out_of_core(
            _plan(),
            sources,
            registry=get_default_registry(),
            relationship_graph=_graph(OrphanPolicy.PRESERVE),
            sink=sink,
            temp_dir=work_root / "runner",
            memory_limit=f"{memory_limit_mb}MB",
        )
        out_table = pq.read_table(target / "orders.parquet", columns=["customer_id"])
        out_rows = out_table.num_rows
        null_out = out_table.column("customer_id").null_count
    finally:
        peak = _peak_rss_mb()
        shutil.rmtree(work_root, ignore_errors=True)
    return {
        "parent_rows": parent_rows,
        "child_rows": child_rows,
        "payload_width": payload_width,
        "memory_limit_mb": memory_limit_mb,
        "peak_rss_mb": round(peak, 1),
        "out_rows": out_rows,
        # Every non-orphan child row must resolve to a non-null masked parent
        # key; a fully-null output would mean the join silently matched
        # nothing (a vacuous "plateau" that never touched the join at all).
        "null_out_rows": null_out,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parent-rows", type=int, required=True)
    ap.add_argument("--child-rows", type=int, required=True)
    ap.add_argument("--payload-width", type=int, default=0)
    ap.add_argument("--memory-limit-mb", type=int, required=True)
    ap.add_argument("--orphan-frac", type=float, default=0.02)
    ap.add_argument("--seed", type=int, default=20260722)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rec = run_one(
        args.parent_rows,
        args.child_rows,
        args.payload_width,
        args.memory_limit_mb,
        args.orphan_frac,
        seed=args.seed,
    )
    if args.json:
        print(json.dumps(rec))
    else:
        print(
            f"parent={rec['parent_rows']:,} child={rec['child_rows']:,} "
            f"width={rec['payload_width']} mem_limit={rec['memory_limit_mb']}MB -> "
            f"peak_rss={rec['peak_rss_mb']:.1f} MB "
            f"(out_rows={rec['out_rows']:,}, null_out={rec['null_out_rows']:,})"
        )


if __name__ == "__main__":
    main()
