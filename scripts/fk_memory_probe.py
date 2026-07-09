"""Measure peak memory of the FK relationships mask paths.

Replaces the linear extrapolation in `docs/v2/perf/engine-v2-baseline-report.md`
(quoted in `docs/relationships-memory-scaling.md`) with a real measurement: build
a parent to child to grandchild FK chain of N rows per table, run it through one
of three routes, and report peak resident memory:

    --mode full         `PandasExecutionAdapter.run`, every table full-width at
                         once (the path the relationships feature originally
                         forced; see module docstring history above).
    --mode sequential    `PandasExecutionAdapter.run_sequential` (Option 2):
                         one table loaded/masked/evicted at a time.
    --mode out_of_core   `run_fk_out_of_core` streaming end to end (Option 4,
                         post-C3): Parquet-backed `LazySource` inputs, bounded
                         batch passes, `ParquetTransactionalSink` output; see
                         `_run_out_of_core`'s docstring.

Headline metric is peak process RSS (VmHWM from /proc/self/status, the
process's own resident high-water mark; see `_peak_rss_mb` for why not
`ru_maxrss`), because that is what determines whether the job OOMs;
`tracemalloc` is reported too but only sees Python-object allocations (it captures
the FK source-to-masked parent map, not the pyarrow/pandas C buffers).
`out_of_core` additionally reports a polled peak temp-disk footprint and a
post-run parity check of both FK edges (parent->child and child->grandchild)
against the committed Parquet output.

Run one tier per process for a clean per-tier high-water mark:

    .venv/bin/python scripts/fk_memory_probe.py --rows 500000
    .venv/bin/python scripts/fk_memory_probe.py --sweep 100000,250000,500000,1000000
    .venv/bin/python scripts/fk_memory_probe.py --sweep 250000,500000,1000000 \\
        --mode out_of_core

Each sweep size runs in a fresh subprocess so its RSS peak is isolated.

Capability proof (Sprint C5): `--mem-cap-mb N` applies a hard
`resource.setrlimit` ceiling to the worker process before it runs (RLIMIT_DATA
by default: on Linux >= 4.7 it covers brk plus private anonymous mmaps, i.e.
the allocations that actually OOM here, without tripping on pyarrow/DuckDB's
reserved-but-untouched address space the way RLIMIT_AS can), and
`--capability` runs all three routes at one operating point in capped
subprocesses over the SAME on-disk Parquet chain:

    .venv/bin/python scripts/fk_memory_probe.py --capability \\
        --rows 400000 --mem-cap-mb 1024

The expected outcome at a cap sized below the resident working set is
full=OOM, sequential=OOM, out_of_core=completed with FK parity intact on both
edges; the measured operating point for this box (400k rows/table, 1,024 MB;
smaller tiers can die at classifier-unknown sites, see section 6.3) is
documented in `docs/relationships-memory-scaling.md` section 6.3 and asserted
by the opt-in `benchmark`-marked test in
`tests/perf/test_out_of_core_memory_sentinel.py`.
"""

from __future__ import annotations

import argparse
import errno
import gc
import itertools
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

# Ensure repo root on sys.path when run as a script (tests/ is importable, as
# scripts/gen_perf_fixtures.py already relies on).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import duckdb  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
from tests.perf_fixtures.fk_relational import (  # noqa: E402
    build_fk_relational,
    lazy_loader,
    lazy_sources,
    make_graph,
    make_plan,
    write_large_fk_chain,
)

from decoy_engine.execution import PandasExecutionAdapter, ParquetTransactionalSink  # noqa: E402
from decoy_engine.execution.out_of_core import resolve_budget, run_fk_out_of_core  # noqa: E402
from decoy_engine.providers_v2 import get_default_registry  # noqa: E402
from decoy_engine.relationships._graph import OrphanPolicy  # noqa: E402
from decoy_engine.relationships._namespace import NamespaceRegistry  # noqa: E402

_POLICIES = {p.name.lower(): p for p in OrphanPolicy}
_N_TABLES = len(make_plan().seed_envelope.per_table)
_TABLE_NAMES = ("parent", "child", "grandchild")
_MIB = 1024 * 1024

_RLIMITS = {"as": resource.RLIMIT_AS, "data": resource.RLIMIT_DATA}

# Failure shapes a hard memory cap produces in this stack. pyarrow's
# ArrowMemoryError and numpy's _ArrayMemoryError both subclass MemoryError;
# DuckDB raises its own OutOfMemoryException; mmap/fork failures surface as
# OSError(ENOMEM).
_MEMORY_ERRORS: tuple[type[BaseException], ...] = (MemoryError, duckdb.OutOfMemoryException)

# Under a hard cap the ceiling-hitting allocation can land inside a C library
# that reports its OWN error instead of MemoryError. The one observed here is
# OpenSSL's EVP_MD_CTX copy (hashlib, masking the FK key): when its malloc
# fails it raises ValueError("[digital envelope routines] not able to copy
# ctx"), never on a healthy run. Which allocation trips first is timing/arena
# dependent (the same job OOMs cleanly as ArrowMemoryError standalone), so this
# is recognized as a memory failure by message, kept specific enough not to
# absorb an unrelated ValueError.
_MEMORY_ERROR_MARKERS = ("not able to copy ctx", "digital envelope routines")

# Second observed under-cap-only shape, same evidence discipline as above:
# Arrow's arrow-to-pandas value conversion, when a single value's PyObject
# allocation returns NULL under the rlimit, clears the Python error and emits
# Status::UnknownError("Wrapping <value> failed") (arrow_to_pandas.cc's
# WrapBytes path), surfacing as ArrowException("Unknown error: Wrapping c35311
# failed") with a DATA VALUE in the middle. Reproduced only under the cap
# (300k rows / 1024 MB, full baseline); the same uncapped job completes with
# no such message. The one theoretical non-memory path to this emission is
# invalid UTF-8 in a string column, which this harness's self-generated ASCII
# fixtures (write_large_fk_chain / build_table) cannot produce. Anchored on
# the full "Unknown error: Wrapping <token> failed" emission, never a bare
# "Unknown error", so an unrelated UnknownError status stays classified failed.
_MEMORY_ERROR_PATTERNS = (re.compile(r"Unknown error: Wrapping \S+ failed"),)

# The share of a hard process cap handed to DuckDB's buffer manager on the
# capped out-of-core route. The interpreter plus the pandas/pyarrow/duckdb
# imports and the route's own bounded Arrow batches live OUTSIDE DuckDB's
# accounting (measured ~300 MB baseline on this stack), so giving DuckDB the
# whole cap would let the two halves together blow the rlimit.
_DUCKDB_CAP_FRACTION = 4

# Allocator pinning for capped workers. Arrow's default (jemalloc/mimalloc)
# and glibc's per-thread arenas reserve address space far beyond live data
# (~2.5 GB measured on this stack at a ~400 MB RSS), and both RLIMIT_AS and
# RLIMIT_DATA count those reservations, so an unpinned capped run OOMs on
# reservation, not on real usage. The system pool plus two glibc arenas make
# the rlimit track actual allocation. Must be in the environment BEFORE the
# worker process starts (glibc reads MALLOC_ARENA_MAX at startup, pyarrow
# reads ARROW_DEFAULT_MEMORY_POOL at import), hence driver-side env, not
# worker-side setenv; set these manually when running a capped single-tier
# probe by hand.
_CAPPED_ENV = {
    "ARROW_DEFAULT_MEMORY_POOL": "system",
    "MALLOC_ARENA_MAX": "2",
}


def _peak_rss_mb() -> float:
    """This process's true peak RSS in MB.

    VmHWM, not `ru_maxrss`: on Linux the rusage high-water mark survives
    execve, so a worker spawned by a large parent (a full pytest session)
    inherits the PARENT's peak and over-reports by hundreds of MB (measured:
    a child of a 600 MB parent reported ru_maxrss ~610 MB vs VmHWM ~9 MB).
    VmHWM belongs to the process's own mm, which exec recreates, so it is
    parent-independent. ru_maxrss stays as the non-Linux fallback (kilobytes
    on Linux, bytes on macOS; measurement boxes here are Linux).
    """
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _peak_vms_mb() -> float | None:
    """Peak virtual address space (VmPeak), the figure RLIMIT_AS caps.

    RSS decides kernel OOM; address space decides rlimit OOM, and the two
    differ by hundreds of MB here (allocator arenas, DuckDB thread stacks,
    reserved-but-untouched mappings), so cap tuning needs both. Linux only.
    """
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmPeak:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return None


def _apply_mem_cap(cap_mb: int, rlimit_kind: str) -> None:
    """Hard-cap this process's memory before any workload allocation."""
    cap_bytes = cap_mb * _MIB
    resource.setrlimit(_RLIMITS[rlimit_kind], (cap_bytes, cap_bytes))


def _is_memory_failure(exc: BaseException) -> bool:
    if isinstance(exc, _MEMORY_ERRORS):
        return True
    if isinstance(exc, OSError) and exc.errno == errno.ENOMEM:
        return True
    message = str(exc)
    if any(marker in message for marker in _MEMORY_ERROR_MARKERS):
        return True
    return any(pattern.search(message) for pattern in _MEMORY_ERROR_PATTERNS)


def _source_paths(source_dir: Path) -> dict[str, Path]:
    return {name: source_dir / f"{name}.parquet" for name in _TABLE_NAMES}


def _verify_fk_sample(fixture, result, sample: int = 2000) -> int:
    """Cheap correctness gate: a sample of non-orphan child FK rows must resolve
    to the masked parent key. Returns the number of rows checked. Builds the
    parent map over the full parent key column (one key column, cheap) so the
    check is not vacuous when child keys span the whole parent range at scale."""
    # Parent map covers the FULL parent key column (a single key column, cheap
    # even at 1M rows), not the first `sample` rows: child parent_id references
    # the whole parent range, so a windowed parent map made the check vacuous at
    # scale (only ~sample/rows links fell in-window, ~2 at 1M). The child side
    # stays sampled to keep the row scan cheap.
    p_src = fixture.sources["parent"].column("id").to_pylist()
    p_msk = result.outputs["parent"].column("id").to_pylist()
    pmap = dict(zip(p_src, p_msk, strict=True))
    child_src = fixture.sources["child"].column("parent_id").to_pylist()[:sample]
    child_msk = result.outputs["child"].column("parent_id").to_pylist()[:sample]
    checked = 0
    for s, m in zip(child_src, child_msk, strict=True):
        if s in pmap:
            if m != pmap[s]:
                raise AssertionError(f"FK broken: {s} masked to {m}, expected {pmap[s]}")
            checked += 1
    if p_src[0] == p_msk[0]:
        raise AssertionError("parent key was not masked")
    return checked


def _fixture_from_dir(source_dir: Path) -> SimpleNamespace:
    """A resident-`sources` fixture read back from an on-disk Parquet chain.

    Deliberately loads every table whole: this backs the full-frame baseline,
    whose defining property is that everything is resident at once.
    """
    return SimpleNamespace(
        sources={name: pq.read_table(path) for name, path in _source_paths(source_dir).items()},
        plan=make_plan(),
        namespace_registry=NamespaceRegistry(bindings=()),
        registry=get_default_registry(),
        graph=make_graph,
    )


def _run_full(rows: int, width: int, orphan_frac: float, policy, source_dir: Path | None) -> dict:
    """Full-frame run: all tables built (or read whole from Parquet) and
    resident, then masked at once."""
    build_t0 = time.perf_counter()
    if source_dir is None:
        fixture = build_fk_relational(rows=rows, width=width, orphan_frac=orphan_frac)
    else:
        fixture = _fixture_from_dir(source_dir)
    build_s = time.perf_counter() - build_t0

    gc.collect()
    tracemalloc.start()
    mask_t0 = time.perf_counter()
    result = PandasExecutionAdapter().run(
        fixture.plan,
        fixture.sources,
        registry=fixture.registry,
        relationship_graph=fixture.graph(policy),
        namespace_registry=fixture.namespace_registry,
    )
    mask_s = time.perf_counter() - mask_t0
    _, tm_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "build_s": round(build_s, 2),
        "mask_s": round(mask_s, 2),
        "tracemalloc_peak_mb": round(tm_peak / _MIB, 1),
        "fk_rows_checked": _verify_fk_sample(fixture, result),
    }


def _run_sequential(
    rows: int, width: int, orphan_frac: float, policy, source_dir: Path | None
) -> dict:
    """Option 2: lazy per-table load + per-table emit + evict, so the three tables
    are never resident at once (each loaded table is still whole while current).
    The sink discards each masked table (keeps only its row count) to measure the
    true single-table ceiling. Correctness is proven by
    tests/unit/execution/test_sequential_eviction.py, so no FK verify here."""
    plan = make_plan()
    if source_dir is None:
        loader = lazy_loader(rows, width=width, orphan_frac=orphan_frac)
    else:
        paths = _source_paths(source_dir)

        def loader(table: str) -> pa.Table:
            return pq.read_table(paths[table])

    rows_seen: dict[str, int] = {}

    def sink(table: str, out: pa.Table) -> None:
        rows_seen[table] = out.num_rows

    gc.collect()
    tracemalloc.start()
    mask_t0 = time.perf_counter()
    PandasExecutionAdapter().run_sequential(
        plan,
        loader,
        registry=get_default_registry(),
        relationship_graph=make_graph(policy),
        namespace_registry=NamespaceRegistry(bindings=()),
        sink=sink,
    )
    mask_s = time.perf_counter() - mask_t0
    _, tm_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "build_s": 0.0,  # lazy build is folded into the masked run
        "mask_s": round(mask_s, 2),
        "tracemalloc_peak_mb": round(tm_peak / _MIB, 1),
        "fk_rows_checked": sum(rows_seen.values()),
    }


class _DiskPeakSampler:
    """Polls total bytes under `root` on a background thread and keeps the max.

    `run_fk_out_of_core` wipes its own relation/join staging subtrees on
    success (`_runner.py`'s `finally` block), so the on-disk high-water mark
    cannot be read after the run, only during it. This is the same
    high-water-mark-by-polling approach as `ru_maxrss` for memory, but with a
    coarser, explicit sampling interval: true peak disk usage could fall
    strictly between two samples and be missed (most exposed by a very short
    burst write/delete inside one polling window). Documented honestly per the
    sprint brief rather than claimed as an exact measurement.
    """

    def __init__(self, root: Path, interval_s: float = 0.1) -> None:
        self._root = root
        self._interval = interval_s
        self._stop = threading.Event()
        self._peak_bytes = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._sample()  # catch a peak that landed after the last loop wakeup

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self._interval)

    def _sample(self) -> None:
        total = 0
        if self._root.exists():
            for dirpath, _dirnames, filenames in os.walk(self._root):
                for name in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, name))
                    except OSError:
                        pass  # file evicted between listdir and stat; not the peak
        self._peak_bytes = max(self._peak_bytes, total)

    @property
    def peak_mb(self) -> float:
        return self._peak_bytes / _MIB


def _head_column_values(path: Path, column: str, n: int) -> list:
    """First `n` values of one Parquet column, read batch-bounded.

    Never materializes the whole column: the capped capability run must not
    have its close-out check reintroduce an O(rows) load.
    """
    values: list = []
    for batch in pq.ParquetFile(path).iter_batches(batch_size=min(n, 8192), columns=[column]):
        values.extend(batch.column(0).to_pylist())
        if len(values) >= n:
            break
    return values[:n]


def _verify_committed_edge(
    parent_src: Path,
    parent_msk: Path,
    key_col: str,
    child_src: Path,
    child_msk: Path,
    fk_col: str,
    sample: int,
) -> int:
    """Head-window parity for one FK edge of a committed run; returns the
    number of real links resolved. Raises AssertionError on a broken link or
    an unmasked parent key, ValueError on a length mismatch (strict zips:
    every admitted orphan policy is row-count- and order-preserving, so a
    mismatch is itself a parity break, not an expected shape)."""
    p_src = _head_column_values(parent_src, key_col, sample)
    p_msk = _head_column_values(parent_msk, key_col, sample)
    c_src = _head_column_values(child_src, fk_col, sample)
    c_msk = _head_column_values(child_msk, fk_col, sample)
    checked = 0
    pmap = dict(zip(p_src, p_msk, strict=True))
    for s, m in zip(c_src, c_msk, strict=True):
        if s in pmap:
            if m != pmap[s]:
                raise AssertionError(
                    f"FK broken on {fk_col}: {s} masked to {m}, expected {pmap[s]}"
                )
            checked += 1
    if p_src and p_src[0] == p_msk[0]:
        raise AssertionError(f"{key_col} parent key was not masked")
    return checked


def _verify_fk_sample_committed(
    src_paths: dict[str, Path], target: Path, sample: int = 2000
) -> tuple[str, dict[str, int]]:
    """Bounded parity check for a sink-published out-of-core run, both FK edges.

    Valid ONLY against `write_large_fk_chain`'s positional key layout (the
    layout every out-of-core probe input uses): parent row i carries key p{i}
    and FK row i references parent i % rows (child parent_id -> p{i % rows},
    grandchild child_id -> c{i % rows}), so the first `sample` FK rows resolve
    entirely inside the first `sample` parent rows (minus the planted
    every-Nth orphans). That is what makes a HEAD-window parent map
    non-vacuous here, where the same window over the eager fixture's sampled
    keys resolved ~nothing (see `_verify_fk_sample`); it is also what keeps
    this check O(sample), never O(rows), so verifying a capped run cannot
    itself blow the memory cap the run just proved it fits.

    Both edges are checked because they break differently: the grandchild's
    parent relation is built from the child's REWRITTEN staged keys, so a
    cap- or spill-specific corruption of that rewrite would be invisible to a
    parent->child-only check (the grandchild edge resolves against the
    COMMITTED child.parquet keys, exactly what the runner staged). Returns
    ("ok"|"MISMATCH", links_resolved_per_edge); a MISMATCH zeroes the counts
    so no caller can treat a broken run as having verified links.
    """
    edges = (
        (
            "parent->child",
            src_paths["parent"],
            target / "parent.parquet",
            "id",
            src_paths["child"],
            target / "child.parquet",
            "parent_id",
        ),
        (
            "child->grandchild",
            src_paths["child"],
            target / "child.parquet",
            "id",
            src_paths["grandchild"],
            target / "grandchild.parquet",
            "child_id",
        ),
    )
    checked: dict[str, int] = {}
    try:
        for edge, parent_src, parent_msk, key_col, child_src, child_msk, fk_col in edges:
            checked[edge] = _verify_committed_edge(
                parent_src, parent_msk, key_col, child_src, child_msk, fk_col, sample
            )
    except (AssertionError, ValueError) as exc:
        # Caught rather than raised so the worker reports MISMATCH as data
        # (the driver would misread a crash here as a route failure).
        print(f"  PARITY MISMATCH: {exc}", file=sys.stderr)
        return "MISMATCH", {}
    return "ok", checked


def _dir_size_mb(path: Path) -> float:
    total = 0
    if path.exists():
        for dirpath, _dirnames, filenames in os.walk(path):
            for name in filenames:
                total += os.path.getsize(os.path.join(dirpath, name))
    return total / _MIB


def _run_out_of_core(
    rows: int,
    width: int,
    orphan_frac: float,
    policy,
    source_dir: Path | None,
    mem_cap_mb: int | None = None,
) -> dict:
    """Option 4 route, streaming end to end (post-C3): each source table lives
    on disk as Parquet and enters `run_fk_out_of_core` as a `LazySource`, so no
    input table is ever whole-resident; the rewrite streams through bounded
    batch passes into a `ParquetTransactionalSink`, and each masked table is
    evicted as soon as its outgoing parent-key relations are built
    (`_runner.py`). Without `--source-dir` the chain is generated straight to
    Parquet one bounded chunk at a time (`write_large_fk_chain`), so even
    fixture construction never holds a whole table. Under `--mem-cap-mb`, the
    DuckDB `memory_limit` and the route's `batch_rows` are derived from the cap
    via `resolve_budget` (Sprint C4), a conservative fraction because the
    interpreter and Arrow batch buffers live outside DuckDB's accounting.
    """
    work_root = Path(tempfile.mkdtemp(prefix="decoy-ooc-probe-"))
    runner_temp = work_root / "runner"
    target = work_root / "output" / "published"
    sink = ParquetTransactionalSink(target)

    # Sample only the runner's transient scratch (relation/join key staging and
    # DuckDB spill, all under runner_temp), NOT the sink's committed full-width
    # output under work_root/output. The committed output is the deliverable,
    # not temp disk, and is reported separately as committed_output_mb; rooting
    # the sampler at work_root would conflate the two and overstate scratch.
    disk_peak = _DiskPeakSampler(runner_temp)
    try:
        build_t0 = time.perf_counter()
        if source_dir is None:
            src_paths = write_large_fk_chain(
                work_root / "src", rows, width=width, orphan_frac=orphan_frac
            )
        else:
            src_paths = _source_paths(source_dir)
        build_s = time.perf_counter() - build_t0

        memory_limit = None
        batch_rows = None
        if mem_cap_mb is not None:
            budget = resolve_budget(budget_bytes=mem_cap_mb * _MIB // _DUCKDB_CAP_FRACTION)
            memory_limit = budget.memory_limit
            batch_rows = budget.batch_rows

        disk_peak.start()
        gc.collect()
        tracemalloc.start()
        mask_t0 = time.perf_counter()
        try:
            run_fk_out_of_core(
                make_plan(),
                lazy_sources(src_paths),
                registry=get_default_registry(),
                relationship_graph=make_graph(policy),
                sink=sink,
                temp_dir=runner_temp,
                memory_limit=memory_limit,
                batch_rows=batch_rows,
            )
        finally:
            mask_s = time.perf_counter() - mask_t0
            _, tm_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            disk_peak.stop()

        parity, checked_by_edge = _verify_fk_sample_committed(src_paths, target)
        committed_mb = _dir_size_mb(target)
    finally:
        # Always remove the whole work tree, including on a run_fk_out_of_core
        # failure, so a failed sweep cell does not leak a temp tree under /tmp.
        shutil.rmtree(work_root, ignore_errors=True)

    return {
        "build_s": round(build_s, 2),
        "mask_s": round(mask_s, 2),
        "tracemalloc_peak_mb": round(tm_peak / _MIB, 1),
        "fk_rows_checked": sum(checked_by_edge.values()),
        "fk_rows_checked_by_edge": checked_by_edge,
        "parity": parity,
        "temp_disk_peak_mb": round(disk_peak.peak_mb, 1),
        "committed_output_mb": round(committed_mb, 1),
        "duckdb_memory_limit": memory_limit,
        "batch_rows": batch_rows,
    }


# out_of_core is dispatched separately (it takes the extra mem_cap_mb arg); this
# table is the two baselines that share the (rows, width, orphan_frac, policy,
# source_dir) signature.
_MODE_RUNNERS = {
    "full": _run_full,
    "sequential": _run_sequential,
}


def _run_one(
    rows: int,
    width: int,
    orphan_frac: float,
    policy_name: str,
    mode: str,
    source_dir: Path | None = None,
    mem_cap_mb: int | None = None,
) -> dict:
    policy = _POLICIES[policy_name]
    header = {
        "mode": mode,
        "rows_per_table": rows,
        "tables": _N_TABLES,
        "width": width,
        "orphan_frac": orphan_frac,
        "orphan_policy": policy_name,
        "mem_cap_mb": mem_cap_mb,
    }
    try:
        if mode == "out_of_core":
            metrics = _run_out_of_core(rows, width, orphan_frac, policy, source_dir, mem_cap_mb)
        else:
            metrics = _MODE_RUNNERS[mode](rows, width, orphan_frac, policy, source_dir)
    except BaseException as exc:
        if mem_cap_mb is not None and _is_memory_failure(exc):
            # The expected shape under a hard cap: report it as data, exit 0.
            return {
                **header,
                "completed": False,
                "error": f"{type(exc).__name__}: {exc}"[:200],
                "peak_rss_mb": round(_peak_rss_mb(), 1),
                "peak_vms_mb": _peak_vms_mb(),
            }
        raise
    return {
        **header,
        "completed": True,
        "peak_rss_mb": round(_peak_rss_mb(), 1),
        "peak_vms_mb": _peak_vms_mb(),
        **metrics,
    }


def _worker_cmd(
    rows: int,
    width: int,
    orphan_frac: float,
    policy_name: str,
    mode: str,
    mem_cap_mb: int | None = None,
    rlimit_kind: str = "data",
    source_dir: Path | None = None,
) -> list[str]:
    cmd = [
        sys.executable,
        os.path.abspath(__file__),
        "--rows",
        str(rows),
        "--width",
        str(width),
        "--orphan-frac",
        str(orphan_frac),
        "--orphan-policy",
        policy_name,
        "--mode",
        mode,
        "--json",
    ]
    if mem_cap_mb is not None:
        cmd += ["--mem-cap-mb", str(mem_cap_mb), "--rlimit-kind", rlimit_kind]
    if source_dir is not None:
        cmd += ["--source-dir", str(source_dir)]
    return cmd


def _sweep(sizes: list[int], width: int, orphan_frac: float, policy_name: str, mode: str) -> None:
    records = []
    for rows in sizes:
        cmd = _worker_cmd(rows, width, orphan_frac, policy_name, mode)
        proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        if proc.returncode != 0 or not line:
            print(f"  rows={rows}: FAILED (rc={proc.returncode})")
            if proc.stderr:
                print("    " + proc.stderr.strip().splitlines()[-1])
            continue
        rec = json.loads(line)
        records.append(rec)
        extra = ""
        if "temp_disk_peak_mb" in rec:
            extra = f"  temp_disk={rec['temp_disk_peak_mb']:>7.1f} MB  parity={rec['parity']}"
        print(
            f"  rows={rec['rows_per_table']:>9,}  "
            f"peak_rss={rec['peak_rss_mb']:>8.1f} MB  "
            f"tracemalloc={rec['tracemalloc_peak_mb']:>7.1f} MB  "
            f"mask={rec['mask_s']:>6.2f}s  build={rec['build_s']:>6.2f}s{extra}"
        )

    if len(records) >= 2:
        print("\nPer-row RSS slope (MB per 1k rows-per-table, full chain of 3 tables):")
        for a, b in itertools.pairwise(records):
            d_rows = b["rows_per_table"] - a["rows_per_table"]
            d_rss = b["peak_rss_mb"] - a["peak_rss_mb"]
            slope = d_rss / (d_rows / 1000.0)
            print(
                f"  {a['rows_per_table']:>9,} to {b['rows_per_table']:>9,}: "
                f"{slope:6.2f} MB / 1k rows"
            )


def _classify_capability_outcome(returncode: int, rec: dict | None, stderr: str) -> str:
    """One of "completed", "oom", "failed" for a capped capability worker.

    A worker that caught a memory failure itself exits 0 with completed=False
    (self-classified via `_is_memory_failure`, the trustworthy path). A worker
    the cap killed harder exits abnormally, and only a memory-shaped abnormal
    exit counts as OOM, so a genuine non-memory bug in a baseline cannot
    masquerade as the expected OOM and silently prove the capability:
    - a Python-level exit (returncode > 0, i.e. an uncaught exception with a
      traceback) is OOM only when a memory marker is in stderr, else failed;
    - a signal death (returncode < 0) is OOM for the memory-pressure signals
      SIGKILL (the kernel OOM killer) and SIGABRT (C++ `bad_alloc` /
      `std::terminate`), or for any signal whose stderr shows a memory marker;
      any other signal (e.g. a bare SIGSEGV/SIGBUS with no memory marker) is a
      likely real crash and classified failed.
    """
    if returncode == 0 and rec is not None:
        return "completed" if rec.get("completed") else "oom"
    memory_markers = (
        "MemoryError",
        "bad_alloc",
        "OutOfMemory",
        "Cannot allocate",
        "ENOMEM",
        "arrow::Status OutOfMemory",
        *_MEMORY_ERROR_MARKERS,
    )
    if any(marker in stderr for marker in memory_markers):
        return "oom"
    if any(pattern.search(stderr) for pattern in _MEMORY_ERROR_PATTERNS):
        return "oom"
    if returncode in (-signal.SIGKILL, -signal.SIGABRT):
        return "oom"
    return "failed"


def _capability(
    rows: int,
    width: int,
    orphan_frac: float,
    policy_name: str,
    mem_cap_mb: int,
    rlimit_kind: str,
) -> int:
    """Run all three routes at one operating point under the same hard cap.

    The Parquet chain is generated once, uncapped and chunk-bounded, then each
    route runs in a fresh capped subprocess over that same on-disk input. The
    capability claim being proven: at a cap below the resident working set,
    the routes that hold whole tables in memory (full, sequential) OOM, while
    the streaming out-of-core route completes with FK parity intact.
    """
    src_root = Path(tempfile.mkdtemp(prefix="decoy-cap-src-"))
    outcomes: dict[str, dict] = {}
    try:
        print(
            f"Capability comparison: rows={rows:,}/table x {_N_TABLES} tables, "
            f"width={width}, orphan_frac={orphan_frac}, policy={policy_name}, "
            f"mem_cap={mem_cap_mb} MB (RLIMIT_{rlimit_kind.upper()})\n"
        )
        write_large_fk_chain(src_root, rows, width=width, orphan_frac=orphan_frac)
        print(f"  source chain: {_dir_size_mb(src_root):.1f} MB Parquet on disk\n")
        for mode in ("full", "sequential", "out_of_core"):
            cmd = _worker_cmd(
                rows,
                width,
                orphan_frac,
                policy_name,
                mode,
                mem_cap_mb=mem_cap_mb,
                rlimit_kind=rlimit_kind,
                source_dir=src_root,
            )
            proc = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,
                env={**os.environ, **_CAPPED_ENV},
            )
            line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
            rec = None
            if line:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    rec = None
            outcome = _classify_capability_outcome(proc.returncode, rec, proc.stderr)
            detail: dict = {"outcome": outcome, "returncode": proc.returncode}
            if rec is not None:
                detail.update(rec)
            elif proc.stderr:
                detail["stderr_tail"] = proc.stderr.strip().splitlines()[-1][:200]
            outcomes[mode] = detail
            summary = f"  {mode:<12} -> {outcome.upper():<10}"
            if outcome == "completed":
                summary += f" peak_rss={detail.get('peak_rss_mb', '?')} MB"
                if "parity" in detail:
                    links = " ".join(
                        f"{edge}={count}"
                        for edge, count in detail.get("fk_rows_checked_by_edge", {}).items()
                    )
                    summary += (
                        f"  parity={detail['parity']}"
                        f"  fk_links[{links}]"
                        f"  temp_disk={detail['temp_disk_peak_mb']} MB"
                    )
            else:
                summary += f" ({detail.get('error') or detail.get('stderr_tail') or f'rc={proc.returncode}'})"
            print(summary)
    finally:
        shutil.rmtree(src_root, ignore_errors=True)

    # Both FK edges must have resolved real links: the grandchild edge is the
    # one whose parent relation is built from the child's rewritten staged
    # keys, so an empty or broken grandchild sample must fail the proof rather
    # than hide behind a healthy parent->child edge.
    edge_links = outcomes["out_of_core"].get("fk_rows_checked_by_edge") or {}
    proven = (
        outcomes["full"]["outcome"] == "oom"
        and outcomes["sequential"]["outcome"] == "oom"
        and outcomes["out_of_core"]["outcome"] == "completed"
        and outcomes["out_of_core"].get("parity") == "ok"
        and len(edge_links) == 2
        and all(count > 0 for count in edge_links.values())
    )
    verdict = (
        "CAPABILITY PROVEN: out-of-core completed where full-frame and sequential OOM"
        if proven
        else "CAPABILITY NOT PROVEN at this operating point"
    )
    print(f"\n{verdict}")
    print(
        json.dumps(
            {
                "capability": {
                    "rows_per_table": rows,
                    "width": width,
                    "orphan_frac": orphan_frac,
                    "orphan_policy": policy_name,
                    "mem_cap_mb": mem_cap_mb,
                    "rlimit_kind": rlimit_kind,
                    "proven": proven,
                    "outcomes": outcomes,
                }
            }
        )
    )
    return 0 if proven else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", type=int, help="rows per table (single-tier run)")
    ap.add_argument("--width", type=int, default=16, help="payload columns per table")
    ap.add_argument("--orphan-frac", type=float, default=0.0)
    ap.add_argument("--orphan-policy", choices=sorted(_POLICIES), default="preserve")
    ap.add_argument(
        "--mode",
        choices=("full", "sequential", "out_of_core"),
        default="full",
        help=(
            "full-frame run (default), Option 2 sequential load+mask+evict, "
            "or Option 4 out-of-core (LazySource inputs streamed to a "
            "ParquetTransactionalSink)"
        ),
    )
    ap.add_argument("--json", action="store_true", help="emit one JSON line")
    ap.add_argument("--sweep", type=str, help="comma-separated row counts")
    ap.add_argument(
        "--source-dir",
        type=Path,
        help="read the parent/child/grandchild chain from this Parquet dir instead of building it",
    )
    ap.add_argument(
        "--mem-cap-mb",
        type=int,
        help="hard-cap this process's memory (resource.setrlimit) before running",
    )
    ap.add_argument(
        "--rlimit-kind",
        choices=sorted(_RLIMITS),
        default="data",
        help=(
            "which rlimit --mem-cap-mb sets; 'data' (default) caps brk+anonymous "
            "mmaps, 'as' caps total address space (blunter: counts reserved-but-"
            "untouched mappings)"
        ),
    )
    ap.add_argument(
        "--capability",
        action="store_true",
        help=(
            "run the C5 capability comparison: all three modes in capped "
            "subprocesses over one shared on-disk chain (requires --rows and "
            "--mem-cap-mb)"
        ),
    )
    args = ap.parse_args()

    if args.capability:
        if args.rows is None or args.mem_cap_mb is None:
            ap.error("--capability requires --rows and --mem-cap-mb")
        raise SystemExit(
            _capability(
                args.rows,
                args.width,
                args.orphan_frac,
                args.orphan_policy,
                args.mem_cap_mb,
                args.rlimit_kind,
            )
        )

    if args.sweep:
        sizes = [int(s) for s in args.sweep.split(",") if s.strip()]
        print(
            f"FK memory sweep: mode={args.mode}, width={args.width}, "
            f"orphan_frac={args.orphan_frac}, policy={args.orphan_policy}, 3-table chain\n"
        )
        _sweep(sizes, args.width, args.orphan_frac, args.orphan_policy, args.mode)
        return

    if args.rows is None:
        ap.error("provide --rows or --sweep")

    if args.mem_cap_mb is not None:
        _apply_mem_cap(args.mem_cap_mb, args.rlimit_kind)

    rec = _run_one(
        args.rows,
        args.width,
        args.orphan_frac,
        args.orphan_policy,
        args.mode,
        source_dir=args.source_dir,
        mem_cap_mb=args.mem_cap_mb,
    )
    if args.json:
        print(json.dumps(rec))
    else:
        for k, v in rec.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()
