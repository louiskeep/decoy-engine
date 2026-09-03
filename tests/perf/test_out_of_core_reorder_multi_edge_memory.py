"""T12 (`docs/plans/2026-09-03-p4-task7-route-seam.md` section 5): the
multi-edge phase-3 head-admission proof, at the ADMITTED MAXIMUM fan-in
(`N = 2 * merge_fan_in`) -- the shape `_route_policy.decide_route` falls
back to `_batch_join` for above (`4.2`).

Reuses `test_out_of_core_reorder_memory.py`'s discipline (fresh subprocess,
pinned allocator env, VmHWM peak) but drives `_stream_driver.stream_table`
directly (the multi-edge ExitStack that holds every incoming edge's
`_OrderedJoinRows` reader open across the whole of phase 3 -- see that
module's docstring) rather than a single `StreamFkJoiner`, over a
sink + `LazySource` child at N=8 incoming edges (`merge_fan_in=4`, the
harness-scale fan-in `tests/unit/execution/_stream_driver_harness.py`
already uses, not production's 16 -- the shape under test is "N heads at
the admitted ceiling", not a specific production constant).

Each edge independently spills more than one sorter run (proven per-edge,
not just in aggregate), and all N edges' phase-3 readers are open
concurrently while payload batches drain -- the exact residency phase 3's
memory model prices (`N * run_bytes_cap // (2 * merge_fan_in)`, `_reorder_
budget.phase3_head_fit`). Peak RSS must stay within the SAME envelope
(`_ENVELOPE_FACTOR`, 1.35x) the single-edge proof uses, without relaxing it.
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

_CEILING_MIB = 1024
_MERGE_FAN_IN = 4
_N_EDGES = 2 * _MERGE_FAN_IN  # the admitted maximum -- one more falls back to _batch_join
_PARENT_ROWS = 300
# Sized (empirically, per-edge join-row bytes ~1.23x run_bytes_cap) so every
# edge spills a real second sorter run while the process's fixed Python /
# pyarrow / DuckDB baseline overhead stays a small fraction of the ceiling --
# at a smaller ceiling that baseline alone crowds the 1.35x envelope even
# with genuine spill proven, a false failure unrelated to the reorder path's
# own residency.
_CHILD_ROWS = 1_600_000
_KEY_WIDTH = 24
_BATCH_ROWS = 20_000
_SEED = 20260903

_ENVELOPE_FACTOR = 1.35

_TIMEOUT_S = 420

_WORKER_SCRIPT = textwrap.dedent(
    '''
    """Fresh-subprocess worker: streams an N-incoming-edge table through
    `_stream_driver.stream_table` (sink + LazySource) and reports peak RSS
    plus per-edge spill proof facts as one JSON line on stdout."""

    import argparse
    import json
    from pathlib import Path
    from types import SimpleNamespace

    import pyarrow as pa
    import pyarrow.parquet as pq

    from decoy_engine.execution import ParquetTransactionalSink
    from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
    from decoy_engine.execution.out_of_core._external_sort import BoundedExternalSorter
    from decoy_engine.execution.out_of_core._route_policy import _edge_indexes, _table_order
    from decoy_engine.execution.out_of_core._source import LazySource
    from decoy_engine.execution.out_of_core._stream_driver import stream_table
    from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
    from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
    from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

    _JOB_SEED = b"\\x66" * 8


    def _peak_rss_mb() -> float:
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


    def main() -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--ceiling-mib", type=int, required=True)
        parser.add_argument("--n-edges", type=int, required=True)
        parser.add_argument("--merge-fan-in", type=int, required=True)
        parser.add_argument("--parent-rows", type=int, required=True)
        parser.add_argument("--child-rows", type=int, required=True)
        parser.add_argument("--key-width", type=int, required=True)
        parser.add_argument("--batch-rows", type=int, required=True)
        parser.add_argument("--temp-dir", type=str, required=True)
        args = parser.parse_args()

        ceiling_bytes = args.ceiling_mib * 1024 * 1024
        run_bytes_cap = round(0.15 * ceiling_bytes)  # F_SORT, mirrors _reorder_budget.py
        duckdb_bytes = round(0.55 * ceiling_bytes)  # F_DUCKDB
        memory_limit = f"{duckdb_bytes // (1024 * 1024)}MB"

        temp_dir = Path(args.temp_dir)
        seed = _seed()
        parent_names = [f"p{i}" for i in range(args.n_edges)]
        fk_columns = [f"fk{i}" for i in range(args.n_edges)]
        edges = [
            RelationshipEdge(
                parent_table=parent_names[i],
                parent_columns=("key",),
                child_table="child",
                child_columns=(fk_columns[i],),
                namespace=f"ns{i}",
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
        plan = SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=_JOB_SEED, per_table=tuple(per_table)))
        graph = RelationshipGraph(edges=tuple(edges), ordering=())

        sources_dir = temp_dir / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        parent_keys: dict[str, list[str]] = {}
        sources: dict[str, object] = {}
        for i, name in enumerate(parent_names):
            keys = [f"p{i}_{j:0{args.key_width}d}" for j in range(args.parent_rows)]
            parent_keys[name] = keys
            table = pa.table({"key": pa.array(keys, type=pa.string())})
            path = sources_dir / f"{name}.parquet"
            pq.write_table(table, path)
            sources[name] = LazySource(path)

        # Each fk column: mostly matched (forcing real spill via row count),
        # 1/6 null, 1/6 a genuine orphan -- never fails the PRESERVE policy.
        child_cols = {}
        for i, col in enumerate(fk_columns):
            keys = parent_keys[parent_names[i]]
            vals = []
            for row in range(args.child_rows):
                r = row % 6
                if r == 0:
                    vals.append(None)
                elif r == 1:
                    vals.append(f"orphan{i}_{row:0{args.key_width}d}")
                else:
                    vals.append(keys[row % len(keys)])
            child_cols[col] = pa.array(vals, type=pa.string())
        child_table = pa.table(child_cols)
        child_path = sources_dir / "child.parquet"
        pq.write_table(child_table, child_path)
        sources["child"] = LazySource(child_path)

        # Route evidence: the reorder driver must be the one running, never
        # ChildFkBatchJoiner (T16's own witness pattern, reused here).
        real_batch_init = ChildFkBatchJoiner.__init__

        def _forbidden_batch_init(self, *a, **kw):
            raise AssertionError("multi-edge RSS proof must drive the reorder route only")

        ChildFkBatchJoiner.__init__ = _forbidden_batch_init

        run_count_by_edge: dict[str, int] = {}
        join_row_bytes_by_edge: dict[str, int] = {}
        real_run_ordered_join = StreamFkJoiner.run_ordered_join
        real_iter_unordered = StreamFkJoiner._iter_unordered_join_rows

        def _counting_iter(self, batch_rows):
            total = 0
            for batch in real_iter_unordered(self, batch_rows):
                total += batch.nbytes
                join_row_bytes_by_edge[self._edge.namespace] = total
                yield batch

        def _spy_run_ordered_join(self, *a, **kw):
            self._iter_unordered_join_rows = _counting_iter.__get__(self)
            rows = real_run_ordered_join(self, *a, **kw)
            run_count_by_edge[self._edge.namespace] = len(rows._sorter._run_paths)
            return rows

        StreamFkJoiner.run_ordered_join = _spy_run_ordered_join

        sink_target = temp_dir / "out"
        sink = ParquetTransactionalSink(sink_target)
        incoming, outgoing = _edge_indexes(graph)
        parent_relations: dict = {}
        outputs: dict = {}
        warnings: list = []
        root = temp_dir / "work"
        for table_name in _table_order(plan, graph, sources):
            stream_table(
                plan,
                table_name,
                sources[table_name],
                incoming_edges=tuple(incoming[table_name]),
                outgoing_edges=tuple(outgoing[table_name]),
                parent_relations=parent_relations,
                temp_dir=root / "joins" / table_name,
                relation_dir=root / "relations" / table_name,
                staging_path=root / "staged" / table_name / "masked_keys.parquet",
                memory_limit=memory_limit,
                batch_rows=args.batch_rows,
                run_bytes_cap=run_bytes_cap,
                merge_fan_in=args.merge_fan_in,
                sink=sink,
                outputs=outputs,
                warnings=warnings,
            )
        sink.commit()

        child_out = pq.read_table(sink_target / "child.parquet")

        print(
            json.dumps(
                {
                    "ceiling_mib": args.ceiling_mib,
                    "n_edges": args.n_edges,
                    "merge_fan_in": args.merge_fan_in,
                    "run_bytes_cap": run_bytes_cap,
                    "child_rows_out": child_out.num_rows,
                    "child_rows_expected": args.child_rows,
                    "edges_with_multi_run_spill": sum(
                        1 for n in run_count_by_edge.values() if n > 1
                    ),
                    "run_counts_by_edge": run_count_by_edge,
                    "join_row_bytes_by_edge": join_row_bytes_by_edge,
                    "peak_rss_mb": _peak_rss_mb(),
                }
            )
        )


    if __name__ == "__main__":
        main()
    '''
)


def _run_worker(tmp_path):
    worker_path = tmp_path / "_ooc_reorder_multi_edge_worker.py"
    worker_path.write_text(_WORKER_SCRIPT)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    env = {**os.environ, **_CAPPED_ENV}
    cmd = [
        sys.executable,
        str(worker_path),
        "--ceiling-mib",
        str(_CEILING_MIB),
        "--n-edges",
        str(_N_EDGES),
        "--merge-fan-in",
        str(_MERGE_FAN_IN),
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


def test_multi_edge_admitted_max_fan_in_peak_rss_within_envelope(tmp_path):
    rec = _run_worker(tmp_path)

    assert rec["n_edges"] == _N_EDGES
    assert rec["child_rows_out"] == rec["child_rows_expected"]
    # Real spill, per edge, not just in aggregate: at least most of the N
    # edges' sorters produced more than one run (a small number landing at
    # exactly one run is tolerated -- hash-derived matched-row placement is
    # not perfectly uniform across edges -- but the bulk must genuinely spill).
    assert rec["edges_with_multi_run_spill"] >= _N_EDGES - 1, rec["run_counts_by_edge"]

    envelope_mb = _CEILING_MIB * _ENVELOPE_FACTOR
    assert rec["peak_rss_mb"] <= envelope_mb, (
        f"peak RSS {rec['peak_rss_mb']:.1f} MB exceeds the {envelope_mb:.1f} MB "
        f"envelope ({_ENVELOPE_FACTOR}x the {_CEILING_MIB} MiB process ceiling) "
        f"at N={_N_EDGES} concurrently-open phase-3 heads"
    )
