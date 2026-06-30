"""Measure peak memory of the full-frame FK relationships mask path.

Replaces the linear extrapolation in `docs/v2/perf/engine-v2-baseline-report.md`
(quoted in `docs/relationships-memory-scaling.md`) with a real measurement: build
a parent to child to grandchild FK chain of N rows per table, run it through
`PandasExecutionAdapter.run` (the path that materializes every table full-width at
once and rejects chunking when relationships are present), and report peak
resident memory.

Headline metric is peak process RSS (`getrusage().ru_maxrss`, the process
lifetime high-water mark), because that is what determines whether the job OOMs;
`tracemalloc` is reported too but only sees Python-object allocations (it captures
the FK source-to-masked parent map, not the pyarrow/pandas C buffers).

Run one tier per process for a clean per-tier high-water mark:

    .venv/bin/python scripts/fk_memory_probe.py --rows 500000
    .venv/bin/python scripts/fk_memory_probe.py --sweep 100000,250000,500000,1000000

Each sweep size runs in a fresh subprocess so its RSS peak is isolated.
"""

from __future__ import annotations

import argparse
import gc
import itertools
import json
import os
import resource
import subprocess
import sys
import time
import tracemalloc

# Ensure repo root on sys.path when run as a script (tests/ is importable, as
# scripts/gen_perf_fixtures.py already relies on).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pyarrow as pa  # noqa: E402
from tests.perf_fixtures.fk_relational import (  # noqa: E402
    build_fk_relational,
    lazy_loader,
    make_graph,
    make_plan,
)

from decoy_engine.execution import PandasExecutionAdapter  # noqa: E402
from decoy_engine.providers_v2 import get_default_registry  # noqa: E402
from decoy_engine.relationships._graph import OrphanPolicy  # noqa: E402
from decoy_engine.relationships._namespace import NamespaceRegistry  # noqa: E402

_POLICIES = {p.name.lower(): p for p in OrphanPolicy}
_N_TABLES = len(make_plan().seed_envelope.per_table)


def _peak_rss_mb() -> float:
    # ru_maxrss is kilobytes on Linux, bytes on macOS. This box is Linux.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _verify_fk_sample(fixture, result, sample: int = 2000) -> int:
    """Cheap correctness gate: a sample of non-orphan child FK rows must resolve
    to the masked parent key. Returns the number of rows checked. Builds a
    parent map over only the first `sample` parent rows to stay cheap at scale."""
    p_src = fixture.sources["parent"].column("id").to_pylist()[:sample]
    p_msk = result.outputs["parent"].column("id").to_pylist()[:sample]
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


def _run_full(rows: int, width: int, orphan_frac: float, policy) -> dict:
    """Full-frame run: all tables built and resident, then masked at once."""
    build_t0 = time.perf_counter()
    fixture = build_fk_relational(rows=rows, width=width, orphan_frac=orphan_frac)
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
        "tracemalloc_peak_mb": round(tm_peak / (1024 * 1024), 1),
        "fk_rows_checked": _verify_fk_sample(fixture, result),
    }


def _run_sequential(rows: int, width: int, orphan_frac: float, policy) -> dict:
    """Option 2: lazy per-table load + per-table emit + evict, so the three tables
    are never resident at once. The sink discards each masked table (keeps only its
    row count) to measure the true single-table ceiling. Correctness is proven by
    tests/unit/execution/test_sequential_eviction.py, so no FK verify here."""
    plan = make_plan()
    loader = lazy_loader(rows, width=width, orphan_frac=orphan_frac)
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
        "tracemalloc_peak_mb": round(tm_peak / (1024 * 1024), 1),
        "fk_rows_checked": sum(rows_seen.values()),
    }


def _run_one(rows: int, width: int, orphan_frac: float, policy_name: str, mode: str) -> dict:
    policy = _POLICIES[policy_name]
    inner = _run_sequential if mode == "sequential" else _run_full
    metrics = inner(rows, width, orphan_frac, policy)
    return {
        "mode": mode,
        "rows_per_table": rows,
        "tables": _N_TABLES,
        "width": width,
        "orphan_frac": orphan_frac,
        "orphan_policy": policy_name,
        "peak_rss_mb": round(_peak_rss_mb(), 1),
        **metrics,
    }


def _sweep(sizes: list[int], width: int, orphan_frac: float, policy_name: str, mode: str) -> None:
    records = []
    for rows in sizes:
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
        proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        if proc.returncode != 0 or not line:
            print(f"  rows={rows}: FAILED (rc={proc.returncode})")
            if proc.stderr:
                print("    " + proc.stderr.strip().splitlines()[-1])
            continue
        rec = json.loads(line)
        records.append(rec)
        print(
            f"  rows={rec['rows_per_table']:>9,}  "
            f"peak_rss={rec['peak_rss_mb']:>8.1f} MB  "
            f"tracemalloc={rec['tracemalloc_peak_mb']:>7.1f} MB  "
            f"mask={rec['mask_s']:>6.2f}s  build={rec['build_s']:>6.2f}s"
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", type=int, help="rows per table (single-tier run)")
    ap.add_argument("--width", type=int, default=16, help="payload columns per table")
    ap.add_argument("--orphan-frac", type=float, default=0.0)
    ap.add_argument("--orphan-policy", choices=sorted(_POLICIES), default="preserve")
    ap.add_argument(
        "--mode",
        choices=("full", "sequential"),
        default="full",
        help="full-frame run (default) or Option 2 sequential load+mask+evict",
    )
    ap.add_argument("--json", action="store_true", help="emit one JSON line")
    ap.add_argument("--sweep", type=str, help="comma-separated row counts")
    args = ap.parse_args()

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

    rec = _run_one(args.rows, args.width, args.orphan_frac, args.orphan_policy, args.mode)
    if args.json:
        print(json.dumps(rec))
    else:
        for k, v in rec.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()
