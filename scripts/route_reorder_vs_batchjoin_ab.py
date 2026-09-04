"""Throwaway A/B measurement: REORDER vs BATCHJOIN out-of-core FK join routes.

Not production code and not a gated test -- a disposable decision tool for
whether the "reorder route" (`StreamFkJoiner.run_ordered_join`: one unordered
join per edge -> `BoundedExternalSorter` -> contiguity guard, then
`JoinRowCursor` + `resolve_batch` to stream the FK output) is worth building
out further, compared against the existing resident-parent route
(`ChildFkBatchJoiner.join_batch`: one LEFT JOIN per child batch against a
buffer-managed parent TEMP TABLE, no sort).

The reorder route's only claimed advantage is removing the O(distinct-parent-
key) resident floor the batch-join route's parent TEMP TABLE imposes, so the
sweep variable is PARENT distinct-key count with a fixed (large) child. Each
route runs its own fresh, allocator-pinned subprocess per sweep point (same
`_CAPPED_ENV` + VmHWM discipline as `tests/perf/test_out_of_core_reorder_
memory.py`, this script's template), with the SAME process ceiling, the SAME
DuckDB `memory_limit` (computed once via `resolve_reorder_budgets` and passed
identically to both), and the SAME parent-relation build -- only the route
differs.

Run: `.venv/bin/python scripts/route_reorder_vs_batchjoin_ab.py`
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

# Allocator pinning, BEFORE each worker subprocess starts -- same reasoning as
# tests/perf/test_ooc_external_sort_memory.py's own `_CAPPED_ENV`.
_CAPPED_ENV = {
    "ARROW_DEFAULT_MEMORY_POOL": "system",
    "MALLOC_ARENA_MAX": "2",
}

_CEILING_MIB = 512
_CHILD_ROWS = 3_000_000
_KEY_WIDTH = 32
_BATCH_ROWS = 20_000
_PARENT_ROWS_SWEEP = (500, 500_000, 2_000_000)

_TIMEOUT_S = 600

# ~8 GiB: the largest sweep point's parent relation, child-key spill, sorter
# runs, and DuckDB spill temp files all land under one work root; below this
# floor the run is INCONCLUSIVE (risk of a disk-exhaustion crash mid-run), not
# a real result, so skip rather than gamble -- same idea as
# tests/perf/test_out_of_core_relation_dedup_memory.py's `_check_disk_floor`.
_DISK_FLOOR_BYTES = 8 * 1024**3

_RESULTS_PATH = Path(__file__).resolve().parent / "route_ab_results.json"

_WORKER_SCRIPT = textwrap.dedent(
    '''
    """Fresh-subprocess worker: runs ONE FK-join route (--route reorder uses
    StreamFkJoiner.run_ordered_join; --route batchjoin uses
    ChildFkBatchJoiner.join_batch) over a synthetic large-child/parent-swept
    fixture and reports peak RSS plus timing/count facts as one JSON line on
    stdout. Throwaway measurement scaffolding -- written to a temp file by the
    driving script and run inside a disposable, allocator-pinned subprocess so
    each run gets a clean VmHWM."""

    import argparse
    import json
    import time
    from pathlib import Path
    from types import SimpleNamespace

    import pyarrow as pa

    from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
    from decoy_engine.execution.out_of_core._mask import mask_table
    from decoy_engine.execution.out_of_core._relation import (
        build_parent_key_relation_from_tables,
    )
    from decoy_engine.execution.out_of_core._reorder_budget import resolve_reorder_budgets
    from decoy_engine.execution.out_of_core._stream_join import (
        ChildKeyLockstepCursor,
        JoinRowCursor,
        StreamFkJoiner,
    )
    from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
    from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

    _JOB_SEED = b"\\x44" * 8


    def _peak_rss_mb() -> float:
        # VmHWM, not ru_maxrss: see test_ooc_external_sort_memory.py's own
        # docstring for why (ru_maxrss survives execve and over-reports under
        # a parent process).
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024.0
        raise RuntimeError("VmHWM not found in /proc/self/status")


    def _seed() -> ColumnSeed:
        return ColumnSeed(
            namespace=None,
            strategy="passthrough",
            provider="passthrough",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=(),
            coherent_with=(),
        )


    def _plan(seed: ColumnSeed):
        return SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=_JOB_SEED,
                per_table=(
                    ("parents", TableSeed(per_column=(("key", seed),), per_group=())),
                    ("children", TableSeed(per_column=(("key", seed),), per_group=())),
                ),
            )
        )


    def _child_key_batch(start, length, parent_keys, key_width):
        keys = []
        for i in range(start, start + length):
            r = i % 6
            if r == 0:
                keys.append(None)  # null FK: never an orphan
            elif r == 1:
                keys.append(f"orphan{i:0{key_width}d}")  # a real, unmatched orphan
            else:
                keys.append(parent_keys[i % len(parent_keys)])
        return pa.record_batch({"key": pa.array(keys, type=pa.string())})


    def _build_relation(args, temp_dir):
        seed = _seed()
        plan = _plan(seed)
        edge = RelationshipEdge(
            parent_table="parents",
            parent_columns=("key",),
            child_table="children",
            child_columns=("key",),
            namespace="ns_route_ab",
            orphan_policy=OrphanPolicy.PRESERVE,
        )
        parent_keys = [f"p{i:0{args.key_width}d}" for i in range(args.parent_rows)]
        parent = pa.table({"key": parent_keys})
        masked_parent = mask_table(plan, edge.parent_table, parent, skip_columns=frozenset())
        relation = build_parent_key_relation_from_tables(
            source_parent=parent,
            masked_parent=masked_parent,
            edge=edge,
            temp_dir=temp_dir / "relation",
        )
        return edge, parent_keys, relation


    def _run_reorder(args, edge, parent_keys, relation, temp_dir, memory_limit, budgets):
        with StreamFkJoiner(
            edge=edge,
            parent_relation=relation,
            child_key_types=(pa.string(),),
            temp_dir=temp_dir / "join",
            memory_limit=memory_limit,
        ) as joiner:
            joiner.begin_staging()
            pos = 0
            while pos < args.child_rows:
                length = min(args.batch_rows, args.child_rows - pos)
                joiner.stage_batch(_child_key_batch(pos, length, parent_keys, args.key_width))
                pos += length
            joiner.finalize_staging()

            with joiner.run_ordered_join(
                args.batch_rows,
                run_bytes_cap=budgets.run_bytes_cap,
                merge_fan_in=budgets.merge_fan_in,
            ) as rows:
                # Stream the resolution in payload-sized batches and discard
                # each one immediately, so this is symmetric with BATCHJOIN
                # (which also never accumulates rewritten output) instead of
                # holding all _CHILD_ROWS resolved rows resident at once.
                cursor = JoinRowCursor(rows, join_columns=edge.child_columns)
                child_cursor = ChildKeyLockstepCursor(joiner.open_child_key_reader())
                offset = 0
                while offset < args.child_rows:
                    take_n = min(args.batch_rows, args.child_rows - offset)
                    slim = cursor.take(take_n, offset)
                    raw = child_cursor.take(take_n, offset)
                    joiner.resolve_batch(slim, raw)  # discarded: symmetric with batchjoin
                    offset += take_n
                cursor.assert_exhausted()
                child_cursor.assert_exhausted()
                child_cursor.close()
        return offset


    def _run_batchjoin(args, edge, parent_keys, relation, temp_dir, memory_limit):
        count = 0
        with ChildFkBatchJoiner(
            edge=edge,
            parent_relation=relation,
            child_key_types=(pa.string(),),
            temp_dir=temp_dir / "join",
            memory_limit=memory_limit,
        ) as joiner:
            pos = 0
            while pos < args.child_rows:
                length = min(args.batch_rows, args.child_rows - pos)
                batch = _child_key_batch(pos, length, parent_keys, args.key_width)
                rewritten, _orphan_count = joiner.join_batch(batch, key_source=batch)
                count += rewritten.num_rows  # discarded: only the row count is kept
                pos += length
        return count


    def main() -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--route", choices=["reorder", "batchjoin"], required=True)
        parser.add_argument("--ceiling-mib", type=int, required=True)
        parser.add_argument("--parent-rows", type=int, required=True)
        parser.add_argument("--child-rows", type=int, required=True)
        parser.add_argument("--key-width", type=int, required=True)
        parser.add_argument("--batch-rows", type=int, required=True)
        parser.add_argument("--temp-dir", type=str, required=True)
        args = parser.parse_args()

        ceiling_bytes = args.ceiling_mib * 1024 * 1024
        big_disk_bytes = 200 * 1024 * 1024 * 1024  # disk is not under test here
        # Computed ONCE, from the SAME process ceiling, for BOTH routes --
        # the critical control this A/B depends on. duckdb_memory_limit_bytes
        # is passed to both StreamFkJoiner and ChildFkBatchJoiner verbatim.
        budgets = resolve_reorder_budgets(ceiling_bytes, big_disk_bytes)
        memory_limit = f"{budgets.duckdb_memory_limit_bytes // (1024 * 1024)}MB"

        temp_dir = Path(args.temp_dir)
        edge, parent_keys, relation = _build_relation(args, temp_dir)

        start = time.monotonic()
        if args.route == "reorder":
            count = _run_reorder(
                args, edge, parent_keys, relation, temp_dir, memory_limit, budgets
            )
        else:
            count = _run_batchjoin(args, edge, parent_keys, relation, temp_dir, memory_limit)
        wall_time_s = time.monotonic() - start

        print(
            json.dumps(
                {
                    "route": args.route,
                    "parent_rows": args.parent_rows,
                    "child_rows": args.child_rows,
                    "duckdb_memory_limit_bytes": budgets.duckdb_memory_limit_bytes,
                    "count": count,
                    "wall_time_s": wall_time_s,
                    "peak_rss_mb": _peak_rss_mb(),
                }
            )
        )


    if __name__ == "__main__":
        main()
    '''
)


def _run_worker(route: str, parent_rows: int, work_root: Path) -> dict:
    worker_path = work_root / f"_worker_{route}_{parent_rows}.py"
    worker_path.write_text(_WORKER_SCRIPT)
    work_dir = work_root / f"work_{route}_{parent_rows}"
    work_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **_CAPPED_ENV}
    cmd = [
        sys.executable,
        str(worker_path),
        "--route",
        route,
        "--ceiling-mib",
        str(_CEILING_MIB),
        "--parent-rows",
        str(parent_rows),
        "--child-rows",
        str(_CHILD_ROWS),
        "--key-width",
        str(_KEY_WIDTH),
        "--batch-rows",
        str(_BATCH_ROWS),
        "--temp-dir",
        str(work_dir),
    ]
    proc = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, timeout=_TIMEOUT_S, env=env
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{route} worker (parent_rows={parent_rows}) failed "
            f"(code {proc.returncode}):\n{proc.stderr}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _disk_floor_ok(path: Path) -> bool:
    return shutil.disk_usage(path).free >= _DISK_FLOOR_BYTES


def _print_table(results: list[dict]) -> None:
    header = (
        f"{'parent_rows':>12} | {'route':<10} | {'peak_rss_mb':>12} | {'wall_s':>8} | {'count':>10}"
    )
    print("\n" + "=" * len(header))
    print(header)
    print("-" * len(header))
    for entry in results:
        for route in ("reorder", "batchjoin"):
            rec = entry[route]
            print(
                f"{entry['parent_rows']:>12} | {route:<10} | {rec['peak_rss_mb']:>12.1f} | "
                f"{rec['wall_time_s']:>8.2f} | {rec['count']:>10}"
            )
    print("=" * len(header))


def main() -> None:
    work_root = Path(tempfile.mkdtemp(prefix="route_ab_"))
    print(f"work root: {work_root}")

    max_parent_rows = max(_PARENT_ROWS_SWEEP)
    results: list[dict] = []
    any_parity_failed = False

    for parent_rows in _PARENT_ROWS_SWEEP:
        if parent_rows == max_parent_rows and not _disk_floor_ok(work_root):
            free_gib = shutil.disk_usage(work_root).free / (1 << 30)
            print(
                f"SKIP parent_rows={parent_rows}: only {free_gib:.1f} GiB free, "
                f"need at least {_DISK_FLOOR_BYTES / (1 << 30):.0f} GiB -- "
                "inconclusive at this size, not a pass"
            )
            continue

        print(f"\n--- parent_rows={parent_rows} ---")
        reorder = _run_worker("reorder", parent_rows, work_root)
        print(
            f"  reorder:   peak_rss={reorder['peak_rss_mb']:.1f} MB  "
            f"wall={reorder['wall_time_s']:.2f}s  count={reorder['count']}"
        )
        batchjoin = _run_worker("batchjoin", parent_rows, work_root)
        print(
            f"  batchjoin: peak_rss={batchjoin['peak_rss_mb']:.1f} MB  "
            f"wall={batchjoin['wall_time_s']:.2f}s  count={batchjoin['count']}"
        )

        parity_ok = reorder["count"] == batchjoin["count"]
        any_parity_failed = any_parity_failed or not parity_ok
        print(
            f"  parity: {'PASS' if parity_ok else 'FAIL'} "
            f"(reorder count={reorder['count']} vs batchjoin count={batchjoin['count']})"
        )

        peak_ratio = reorder["peak_rss_mb"] / batchjoin["peak_rss_mb"]
        wall_ratio = reorder["wall_time_s"] / batchjoin["wall_time_s"]
        print(
            f"  ratio: reorder_peak/batchjoin_peak={peak_ratio:.3f}  "
            f"reorder_wall/batchjoin_wall={wall_ratio:.3f}"
        )

        results.append(
            {
                "parent_rows": parent_rows,
                "reorder": reorder,
                "batchjoin": batchjoin,
                "parity_ok": parity_ok,
                "peak_ratio": peak_ratio,
                "wall_ratio": wall_ratio,
            }
        )

    _print_table(results)

    _RESULTS_PATH.write_text(json.dumps(results, indent=2))
    print(f"\nraw results written to {_RESULTS_PATH}")

    if any_parity_failed:
        print("\nFAIL: at least one sweep point produced mismatched row counts", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
