"""P4-A.3 Task D: the measured (real-RSS) never-OOM proof for the reorder path.

Mirrors `tests/perf/test_ooc_external_sort_memory.py`'s discipline exactly: a
FRESH subprocess (allocator env pinned, same reasoning) streams a large-child /
small-parent single FK edge through `StreamFkJoiner.run_ordered_join`, with
budgets sized by `resolve_reorder_budgets(process_ceiling_bytes=...)`, and
asserts the subprocess's VmHWM stays within the same documented envelope
(`ENVELOPE_FACTOR (1.35) * process_ceiling_bytes`) while proving real spill
occurred (more than one sorter run). This is the reorder-path counterpart of
the M1 sorter's own proof: the DuckDB unordered join is closed BEFORE the
merge runs (`run_ordered_join`'s own contract), so the two phases this test's
envelope has to absorb -- DRAIN (DuckDB join buffers + sorter write buffer,
co-resident) and MERGE (sorter merge buffers alone) -- are exactly the ones
the P4-A.3 plan's memory contract (S4) describes.

The parent relation is deliberately SMALL (a few hundred rows): this slice's
never-OOM claim is scoped to the REORDER (child/join-output) step, not the
parent-relation dedup (Task 4, deferred), so a large-parent proof would be
measuring a different, not-yet-bounded thing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.perf

# Allocator pinning, BEFORE the worker process starts -- identical reasoning
# to test_ooc_external_sort_memory.py's own `_CAPPED_ENV`.
_CAPPED_ENV = {
    "ARROW_DEFAULT_MEMORY_POOL": "system",
    "MALLOC_ARENA_MAX": "2",
}

# Same named process ceiling the M1 sorter RSS test uses, so the two proofs
# are directly comparable.
_CEILING_MIB = 512

# A small parent (bounded dedup relation) and a large child, sized so the
# SLIM join-row bytes actually flowing into the sorter comfortably exceed
# run_bytes_cap (F_SORT * ceiling, ~77 MiB at this ceiling) by more than 5x --
# proven a real multi-run spill, not an accidental single-buffer pass -- while
# staying disk-safe on a shared devbox. The slim sort carries only row_nr, the
# match token, and the masked key (the raw child columns no longer ride through
# it), so the per-row sort bytes dropped ~4x versus the pre-slim raw join row.
# The sort volume still exceeds run_bytes_cap (forcing a real multi-run spill),
# but no longer by 5x -- and it cannot be pushed back to 5x without inflating
# DuckDB's OWN join-phase peak (more rows) or the masked value's transit (wider
# values) past the RSS envelope, neither of which is the sorter residency under
# test. So the sizing is unchanged from the pre-slim proof and the spill
# assertion tracks the real slim volume instead (~100 MB of slim join-row bytes,
# ~1.3x run_bytes_cap, two initial runs).
_PARENT_ROWS = 500
_CHILD_ROWS = 3_000_000
_KEY_WIDTH = 32
_BATCH_ROWS = 20_000
_SEED = 20260902

_ENVELOPE_FACTOR = 1.35

_TIMEOUT_S = 300

_WORKER_SCRIPT = textwrap.dedent(
    '''
    """Fresh-subprocess worker: streams a large-child/small-parent FK edge
    through StreamFkJoiner.run_ordered_join and reports peak RSS plus proof
    facts as one JSON line on stdout. Not a committed module -- written to a
    temp file by the perf test that drives it, so it is exercised only inside
    a pinned, disposable subprocess."""

    import argparse
    import json
    import time
    from pathlib import Path
    from types import SimpleNamespace

    import pyarrow as pa

    from decoy_engine.execution.out_of_core._mask import mask_table
    from decoy_engine.execution.out_of_core._relation import (
        build_parent_key_relation_from_tables,
    )
    from decoy_engine.execution.out_of_core._reorder_budget import resolve_reorder_budgets
    from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
    from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
    from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

    ROW_NR = "__decoy_row_nr"
    _JOB_SEED = b"\\x44" * 8


    def _peak_rss_mb() -> float:
        # VmHWM, not ru_maxrss: see test_ooc_external_sort_memory.py's own
        # docstring for why (ru_maxrss survives execve and over-reports under
        # pytest's own parent process).
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


    def main() -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--ceiling-mib", type=int, required=True)
        parser.add_argument("--parent-rows", type=int, required=True)
        parser.add_argument("--child-rows", type=int, required=True)
        parser.add_argument("--key-width", type=int, required=True)
        parser.add_argument("--batch-rows", type=int, required=True)
        parser.add_argument("--temp-dir", type=str, required=True)
        args = parser.parse_args()

        ceiling_bytes = args.ceiling_mib * 1024 * 1024
        big_disk_bytes = 200 * 1024 * 1024 * 1024  # disk is not under test here
        budgets = resolve_reorder_budgets(ceiling_bytes, big_disk_bytes)

        temp_dir = Path(args.temp_dir)
        seed = _seed()
        plan = _plan(seed)
        edge = RelationshipEdge(
            parent_table="parents",
            parent_columns=("key",),
            child_table="children",
            child_columns=("key",),
            namespace="ns_reorder_perf",
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

        start = time.monotonic()
        with StreamFkJoiner(
            edge=edge,
            parent_relation=relation,
            child_key_types=(pa.string(),),
            temp_dir=temp_dir / "join",
            memory_limit=f"{budgets.duckdb_memory_limit_bytes // (1024 * 1024)}MB",
        ) as joiner:
            joiner.begin_staging()
            pos = 0
            while pos < args.child_rows:
                length = min(args.batch_rows, args.child_rows - pos)
                joiner.stage_batch(
                    _child_key_batch(pos, length, parent_keys, args.key_width)
                )
                pos += length
            joiner.finalize_staging()

            total_join_row_bytes = 0
            original_iter = joiner._iter_unordered_join_rows

            def _counting(batch_rows):
                nonlocal total_join_row_bytes
                for batch in original_iter(batch_rows):
                    total_join_row_bytes += batch.nbytes
                    yield batch

            joiner._iter_unordered_join_rows = _counting

            with joiner.run_ordered_join(
                args.batch_rows,
                run_bytes_cap=budgets.run_bytes_cap,
                merge_fan_in=budgets.merge_fan_in,
            ) as rows:
                initial_run_count = None  # captured after the eager drain, below
                sorter = rows._sorter
                # The eager drain already ran (inside run_ordered_join, before
                # this `with` block's body starts) -- _run_paths reflects every
                # run finish() merged from, so this is the pre-merge run count.
                initial_run_count = len(sorter._run_paths)

                count = 0
                first_value = None
                last_value = None
                sorted_ok = True
                prev = -1
                for batch in rows:
                    values = batch.column(ROW_NR).to_pylist()
                    for value in values:
                        if first_value is None:
                            first_value = value
                        if value <= prev:
                            sorted_ok = False
                        prev = value
                        count += 1
                    if values:
                        last_value = values[-1]

        wall_time_s = time.monotonic() - start
        sorted_ok = (
            sorted_ok
            and count == args.child_rows
            and first_value == 0
            and last_value == args.child_rows - 1
        )

        print(
            json.dumps(
                {
                    "ceiling_mib": args.ceiling_mib,
                    "run_bytes_cap": budgets.run_bytes_cap,
                    "duckdb_memory_limit_bytes": budgets.duckdb_memory_limit_bytes,
                    "merge_fan_in": budgets.merge_fan_in,
                    "child_rows": args.child_rows,
                    "total_join_row_bytes": total_join_row_bytes,
                    "initial_run_count": initial_run_count,
                    "real_spill": initial_run_count > 1,
                    "sorted_ok": sorted_ok,
                    "count": count,
                    "first_value": first_value,
                    "last_value": last_value,
                    "wall_time_s": wall_time_s,
                    "peak_rss_mb": _peak_rss_mb(),
                }
            )
        )


    if __name__ == "__main__":
        main()
    '''
)


def _run_worker(tmp_path) -> dict:
    worker_path = tmp_path / "_ooc_reorder_memory_worker.py"
    worker_path.write_text(_WORKER_SCRIPT)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    env = {**os.environ, **_CAPPED_ENV}
    cmd = [
        sys.executable,
        str(worker_path),
        "--ceiling-mib",
        str(_CEILING_MIB),
        "--parent-rows",
        str(_PARENT_ROWS),
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
    assert proc.returncode == 0, (
        f"worker subprocess failed (code {proc.returncode}):\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_reorder_peak_rss_within_envelope_while_far_exceeding_ceiling(tmp_path):
    rec = _run_worker(tmp_path)

    assert rec["sorted_ok"] is True, rec
    assert rec["count"] == _CHILD_ROWS
    assert rec["real_spill"] is True, "expected multiple sorter runs, got a single buffered run"
    assert rec["initial_run_count"] > 1

    # The SLIM join-row bytes flowing into the sorter must genuinely exceed
    # run_bytes_cap for this to be a real spill proof, not an accidental
    # single-buffer pass. The slim row is ~4x narrower than the pre-slim raw
    # join row, so this margin is ~1.3x (not the pre-slim 5x); the multi-run
    # `initial_run_count > 1` assertion above is the primary spill proof.
    assert rec["total_join_row_bytes"] > rec["run_bytes_cap"] * 1.15

    envelope_bytes = _CEILING_MIB * _ENVELOPE_FACTOR
    assert rec["peak_rss_mb"] <= envelope_bytes, (
        f"peak RSS {rec['peak_rss_mb']:.1f} MB exceeds the {envelope_bytes:.1f} MB "
        f"envelope ({_ENVELOPE_FACTOR}x the {_CEILING_MIB} MiB process ceiling)"
    )
