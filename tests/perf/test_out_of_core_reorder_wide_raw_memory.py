"""P4 Task 7 slim-sort: end-to-end RSS proof that a multi-MB RAW child value no
longer drives sorter residency.

Before the slim sort, the reorder sorter carried the raw child columns
(`__decoy_fk_join_key`, `__decoy_src_*`) at source width, so a child key wider
than the per-merge-head cap (`run_bytes_cap // (2 * merge_fan_in)`) either
overflowed the sorter (`out_of_core_sort_row_too_wide`) or drove its resident
peak with the raw width. After the fix the sorter carries only the SLIM row
(row_nr + match token + masked key); the raw child columns are re-fetched
out-of-line from `SpillChildKeys` in phase 3, one batch at a time.

These subprocess RSS proofs feed the PRODUCTION `run_fk_out_of_core` entry point
a HASH-masked child whose raw FK values are far wider than the per-merge-head
cap (so the pre-slim sorter would have rejected them), and assert:

- the reorder route is SELECTED (the masked hash is small, so the width gate
  admits it) -- `StreamFkJoiner` per edge, `ChildFkBatchJoiner` never;
- output is complete;
- peak RSS stays within the same 1.35x envelope the narrow-key proofs use --
  proving the multi-MB raw width no longer drives the sorter, and the phase-3
  lockstep raw re-reads stay O(one batch).

Covered at N=1 (one multi-MB raw value) and at the admitted maximum fan-in
(N = 2 * merge_fan_in, overlapping edges).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.perf

_CAPPED_ENV = {
    "ARROW_DEFAULT_MEMORY_POOL": "system",
    "MALLOC_ARENA_MAX": "2",
}

_CEILING_MIB = 512
_MERGE_FAN_IN = 4
_ENVELOPE_FACTOR = 1.35
_TIMEOUT_S = 300

# The raw FK value width. run_bytes_cap = F_SORT * ceiling ~= 77 MiB at this
# ceiling, so the per-merge-head cap at fan-in 4 is ~9.6 MiB; a 16 MiB raw value
# is comfortably OVER it, so the pre-slim sorter (which carried the raw src)
# would have raised `out_of_core_sort_row_too_wide` on it. The hash mask makes
# the SLIM row tiny, so the reorder route admits and completes.
_WIDE_BYTES = 16 * 1024 * 1024
_WIDE_PER_EDGE = 3  # a handful of multi-MB raw values per edge
_NARROW_ROWS = 300  # narrow filler rows so the join is a real (not trivial) join
_BATCH_ROWS = 40  # small, so at most ~1 wide value transits per batch

_WORKER_SCRIPT = textwrap.dedent(
    '''
    """Fresh-subprocess worker: streams a HASH-masked child with multi-MB raw FK
    values through the production `run_fk_out_of_core`, and reports peak RSS plus
    route-selection proof as one JSON line on stdout."""

    import argparse
    import json
    from pathlib import Path
    from types import SimpleNamespace

    import pyarrow as pa
    import pyarrow.parquet as pq

    from decoy_engine.execution import ParquetTransactionalSink
    from decoy_engine.execution.out_of_core import run_fk_out_of_core
    from decoy_engine.execution.out_of_core import _runner as runner_mod
    from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
    from decoy_engine.execution.out_of_core._source import LazySource
    from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
    from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

    _JOB_SEED = b"\\x77" * 8


    def _peak_rss_mb() -> float:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024.0
        raise RuntimeError("VmHWM not found in /proc/self/status")


    def _hash_seed() -> ColumnSeed:
        return ColumnSeed(
            namespace="ns",
            strategy="hash",
            provider="hash",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=True,
            provider_config=(),
            coherent_with=(),
        )


    def _key(i, wide):
        # A wide (multi-MB) value or a narrow one; both are matched parent keys.
        return ("w%d_" % i) + ("x" * wide) if wide else "k%d" % i


    def main() -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--ceiling-mib", type=int, required=True)
        parser.add_argument("--merge-fan-in", type=int, required=True)
        parser.add_argument("--n-edges", type=int, required=True)
        parser.add_argument("--wide-bytes", type=int, required=True)
        parser.add_argument("--wide-per-edge", type=int, required=True)
        parser.add_argument("--narrow-rows", type=int, required=True)
        parser.add_argument("--batch-rows", type=int, required=True)
        parser.add_argument("--temp-dir", type=str, required=True)
        args = parser.parse_args()

        ceiling_bytes = args.ceiling_mib * 1024 * 1024
        big_disk_bytes = 200 * 1024 * 1024 * 1024

        temp_dir = Path(args.temp_dir)
        sources_dir = temp_dir / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        seed = _hash_seed()

        parent_names = ["p%d" % i for i in range(args.n_edges)]
        fk_columns = ["fk%d" % i for i in range(args.n_edges)]
        edges = [
            RelationshipEdge(
                parent_table=parent_names[i],
                parent_columns=("key",),
                child_table="child",
                child_columns=(fk_columns[i],),
                namespace="ns%d" % i,
                orphan_policy=OrphanPolicy.PRESERVE,
            )
            for i in range(args.n_edges)
        ]
        per_table = [
            (name, TableSeed(per_column=(("key", seed),), per_group=())) for name in parent_names
        ]
        per_table.append(
            ("child", TableSeed(per_column=tuple((c, seed) for c in fk_columns), per_group=()))
        )
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(job_seed=_JOB_SEED, per_table=tuple(per_table))
        )
        graph = RelationshipGraph(edges=tuple(edges), ordering=())

        # Each edge's parent holds the same key domain the child references: the
        # wide keys plus narrow ones. Parents are tiny (few rows), so no wide
        # value is ever resident more than briefly.
        total_rows = args.narrow_rows + args.wide_per_edge
        wide_positions = {}
        for i in range(args.n_edges):
            # Stagger each edge's wide rows so a single source batch rarely holds
            # wide values from many columns at once.
            wide_positions[fk_columns[i]] = {
                (i + k * max(1, args.narrow_rows // args.wide_per_edge)) % total_rows
                for k in range(args.wide_per_edge)
            }

        sources = {}
        for i, name in enumerate(parent_names):
            col = fk_columns[i]
            keys = [_key(r, r in wide_positions[col]) for r in range(total_rows)]
            table = pa.table({"key": pa.array(keys, type=pa.string())})
            path = sources_dir / (name + ".parquet")
            pq.write_table(table, path)
            sources[name] = LazySource(path)
            del keys, table

        # Write the child parquet in row-group batches so the whole wide-valued
        # table is never fully resident during setup (that would pollute VmHWM).
        child_path = sources_dir / "child.parquet"
        writer = None
        schema = pa.schema([(c, pa.string()) for c in fk_columns])
        for start in range(0, total_rows, args.batch_rows):
            stop = min(start + args.batch_rows, total_rows)
            cols = {}
            for i, col in enumerate(fk_columns):
                cols[col] = pa.array(
                    [_key(r, r in wide_positions[col]) for r in range(start, stop)],
                    type=pa.string(),
                )
            batch = pa.record_batch(cols, schema=schema)
            if writer is None:
                writer = pq.ParquetWriter(child_path, schema)
            writer.write_batch(batch)
            del cols, batch
        if writer is not None:
            writer.close()
        sources["child"] = LazySource(child_path)

        real_batch_init = ChildFkBatchJoiner.__init__

        def _forbidden_batch_init(self, *a, **kw):
            raise AssertionError("wide-raw RSS proof must drive the reorder route only")

        ChildFkBatchJoiner.__init__ = _forbidden_batch_init

        real_stream_init = StreamFkJoiner.__init__
        stream_init_count = {"n": 0}

        def _counting_stream_init(self, *a, **kw):
            stream_init_count["n"] += 1
            return real_stream_init(self, *a, **kw)

        StreamFkJoiner.__init__ = _counting_stream_init

        real_decide_route = runner_mod.decide_route
        captured = {}

        def _pinned_decide_route(*a, **kw):
            decision = real_decide_route(*a, merge_fan_in=args.merge_fan_in, **kw)
            if decision.reorder_caps is not None:
                captured["run_bytes_cap"] = decision.reorder_caps.run_bytes_cap
                captured["merge_fan_in"] = decision.reorder_caps.merge_fan_in
            return decision

        runner_mod.decide_route = _pinned_decide_route

        sink_target = temp_dir / "out"
        run_fk_out_of_core(
            plan,
            sources,
            registry=get_default_registry(),
            relationship_graph=graph,
            sink=ParquetTransactionalSink(sink_target),
            temp_dir=temp_dir / "work",
            batch_rows=args.batch_rows,
            budget_bytes=ceiling_bytes,
            temp_disk_budget_bytes=big_disk_bytes,
            out_of_core_reorder_threshold_rows=0,
        )

        child_out = pq.read_table(sink_target / "child.parquet")
        per_head_cap = None
        if captured.get("run_bytes_cap") is not None:
            per_head_cap = captured["run_bytes_cap"] // (2 * captured["merge_fan_in"])

        print(
            json.dumps(
                {
                    "n_edges": args.n_edges,
                    "stream_joiner_init_count": stream_init_count["n"],
                    "resolved_run_bytes_cap": captured.get("run_bytes_cap"),
                    "per_head_cap": per_head_cap,
                    "wide_bytes": args.wide_bytes,
                    "child_rows_out": child_out.num_rows,
                    "child_rows_expected": total_rows,
                    "peak_rss_mb": _peak_rss_mb(),
                }
            )
        )


    if __name__ == "__main__":
        main()
    '''
)


def _run_worker(tmp_path, n_edges):
    worker_path = tmp_path / "_ooc_reorder_wide_raw_worker.py"
    worker_path.write_text(_WORKER_SCRIPT)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    env = {**os.environ, **_CAPPED_ENV}
    cmd = [
        sys.executable,
        str(worker_path),
        "--ceiling-mib",
        str(_CEILING_MIB),
        "--merge-fan-in",
        str(_MERGE_FAN_IN),
        "--n-edges",
        str(n_edges),
        "--wide-bytes",
        str(_WIDE_BYTES),
        "--wide-per-edge",
        str(_WIDE_PER_EDGE),
        "--narrow-rows",
        str(_NARROW_ROWS),
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


def _assert_within_envelope(rec, n_edges):
    assert rec["n_edges"] == n_edges
    # Route selection: every edge built a real StreamFkJoiner (never
    # ChildFkBatchJoiner -- the worker raises if one is constructed), proving the
    # width gate ADMITTED the wide-raw/small-masked edge to the reorder route.
    assert rec["stream_joiner_init_count"] == n_edges, rec
    assert rec["resolved_run_bytes_cap"] is not None, "decide_route never returned reorder_caps"
    # The raw value really is wider than the per-merge-head cap the pre-slim
    # sorter measured against, so this is a genuine "would-have-rejected" case.
    assert rec["wide_bytes"] > rec["per_head_cap"], rec
    assert rec["child_rows_out"] == rec["child_rows_expected"]

    envelope_mb = _CEILING_MIB * _ENVELOPE_FACTOR
    assert rec["peak_rss_mb"] <= envelope_mb, (
        f"peak RSS {rec['peak_rss_mb']:.1f} MB exceeds the {envelope_mb:.1f} MB envelope "
        f"({_ENVELOPE_FACTOR}x the {_CEILING_MIB} MiB ceiling) with {rec['wide_bytes']}-byte "
        f"raw FK values ({rec['per_head_cap']}-byte per-merge-head cap)"
    )


def test_single_edge_multi_mb_raw_peak_rss_within_envelope(tmp_path):
    _assert_within_envelope(_run_worker(tmp_path, n_edges=1), n_edges=1)


def test_max_fan_in_overlapping_multi_mb_raw_peak_rss_within_envelope(tmp_path):
    _assert_within_envelope(
        _run_worker(tmp_path, n_edges=2 * _MERGE_FAN_IN), n_edges=2 * _MERGE_FAN_IN
    )
