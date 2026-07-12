"""TB-1 (docs/plans/2026-07-12-track-b-completion-program.md): the governed
out-of-core path's input+output residency fixes.

Two defects this program's foundational sprint (TB-1) fixes, both in
`_isolated_worker.py`:

  1. `_load_sources` used to `pq.read_table` every manifest path eagerly,
     materializing a relationship-bearing job's whole input in the child
     BEFORE `run_pipeline` ever decided a route -- the root cause of the
     governed out-of-core path's measured ~2x memory overhead (docs plan's
     "two defects" section, #56).
  2. The worker never passed a sink, so a streaming route (out_of_core,
     sequential) still returned its output fully resident before the
     worker wrote it to disk via `pq.write_table` -- no different, memory-
     wise, than the full_frame route it was supposed to beat.

These tests exercise `_isolated_worker` directly (no subprocess spawn), so
they stay fast and deterministic; `test_isolated_run.py` already covers the
real-subprocess spawn/classify machinery this module does not touch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution import _isolated_worker
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.profile._readers import LazySource

_N = 30


def _hash_col(name: str, namespace: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _write_source(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _fk_config(tmp_path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """A small pure-mask FK job whose strategies the out-of-core compat gate
    admits -- mirrors `test_out_of_core_routing.py`'s `_fk_ooc_config`,
    duplicated locally per this test package's existing per-file convention
    (see `test_governor.py`'s module docstring)."""
    parent = pa.table({"id": pa.array([f"p{i}" for i in range(_N)], type=pa.string())})
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(_N)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
        }
    )
    parent_path = _write_source(tmp_path, parent, "parent")
    child_path = _write_source(tmp_path, child, "child")
    config = {
        "version": 1,
        "global_settings": {"job_name": "tb1-worker-test", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_path, "format": "parquet"},
            "child": {"type": "file", "path": child_path, "format": "parquet"},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {"name": "parent", "columns": [_hash_col("id", "ns")]},
            {"name": "child", "columns": [_hash_col("cid", "cns"), _hash_col("parent_id", "ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }
    manifest = {"parent": parent_path, "child": child_path}
    return config, manifest


class TestLoadSourcesLazyScope:
    """Fix #1's `lazy` scoping: relationship-bearing jobs get a `LazySource`;
    everything else keeps the pre-TB-1 eager load."""

    def test_lazy_true_wraps_lazy_source(self, tmp_path: Path) -> None:
        """FAILS pre-TB-1: `_load_sources` always called `pq.read_table`,
        returning a resident `pa.Table` regardless of any `lazy` intent."""
        path = tmp_path / "t.parquet"
        pq.write_table(pa.table({"a": [1, 2, 3]}), path)

        loaded = _isolated_worker._load_sources({"t": str(path)}, lazy=True)

        assert isinstance(loaded["t"], LazySource)
        # Cheap Parquet-footer metadata read only -- the whole point of
        # LazySource: no column data has been touched.
        assert loaded["t"].num_rows == 3

    def test_lazy_false_stays_eager(self, tmp_path: Path) -> None:
        """A non-relationship job keeps the pre-TB-1 eager load. Without this
        scoping, the S3 auto-chunk classifier (`_planner.
        _runtime_source_rejections`) would see a `LazySource` for a job that
        can never reach out-of-core anyway, and would conservatively decline
        the auto-chunk optimization it previously took -- reproduced during
        this sprint's own verification as a real memory-cap regression (a
        200k-row single-table mask job that used to auto-chunk into a
        bounded 50k-row pandas working set instead ran full-frame and
        crashed under a low rlimit cap)."""
        path = tmp_path / "t.parquet"
        pq.write_table(pa.table({"a": [1, 2, 3]}), path)

        loaded = _isolated_worker._load_sources({"t": str(path)}, lazy=False)

        assert isinstance(loaded["t"], pa.Table)
        assert loaded["t"].num_rows == 3


class TestIsolatedWorkerStreamsOutOfCore:
    def test_run_streams_input_and_output_for_out_of_core_route(self, tmp_path: Path) -> None:
        """End-to-end (no subprocess) proof of TB-1 fixes #1 + #2 together,
        exercising the real `_run` the isolated child process calls.

        FAILS pre-TB-1: the worker never passed a sink to `run_pipeline`, so
        `quality_metrics["execution"]["outputs_streamed"]` was always
        `False` for the out-of-core route -- the route reassembled its
        output fully resident in the child before `_stage_outputs` wrote it
        via `pq.write_table`, exactly the residency this sprint removes.
        """
        config, manifest = _fk_config(tmp_path)
        staging_output_dir = tmp_path / "staged"

        # Oracle: full_frame over the SAME resident sources, for byte parity.
        oracle_sources = {name: pq.read_table(path) for name, path in manifest.items()}
        oracle = run_pipeline(
            config, oracle_sources, engine_version="tb1-worker-test", execution_mode="full_frame"
        )

        payload = {
            "config": config,
            "sources": manifest,
            "kwargs": {"execution_mode": "out_of_core", "engine_version": "tb1-worker-test"},
            "mem_cap_bytes": None,
            "rlimit_kind": "data",
            "staging_output_dir": str(staging_output_dir),
        }

        envelope = _isolated_worker._run(payload)

        assert envelope["outcome"] == "completed"
        # The mechanism proof: outputs streamed straight to the sink's
        # target dir, never reassembled resident in this worker.
        assert envelope["quality_metrics"]["execution"]["outputs_streamed"] is True
        assert sorted(envelope["staged_tables"]) == ["child", "parent"]

        # Correctness: what actually landed on disk matches the full_frame
        # oracle byte-for-byte.
        for table in ("parent", "child"):
            sunk = pq.read_table(staging_output_dir / f"{table}.parquet")
            assert sunk.to_pydict() == oracle.outputs[table].to_pydict(), f"{table} sink differs"

    def test_run_full_frame_route_unaffected_by_always_passed_sink(self, tmp_path: Path) -> None:
        """A non-relationship (full_frame-only) job must behave byte-for-byte
        as before: the sink `_run` now always constructs is never touched by
        that route (`_pipeline_route_exec`'s full_frame continuation never
        references `sink`), so outputs stay resident and are staged via the
        unchanged `_stage_outputs` path."""
        path = tmp_path / "t.parquet"
        pq.write_table(pa.table({"a": ["x", "y", "z"]}), path)
        config = {
            "version": 1,
            "global_settings": {"job_name": "tb1-full-frame", "seed": 1},
            "sources": {"t": {"type": "file", "path": str(path), "format": "parquet"}},
            "targets": {
                "t": {"type": "file", "path": str(tmp_path / "t.out.parquet"), "format": "parquet"}
            },
            "tables": [{"name": "t", "columns": [{"name": "a", "strategy": "redact"}]}],
        }
        staging_output_dir = tmp_path / "staged"
        payload = {
            "config": config,
            "sources": {"t": str(path)},
            "kwargs": {"engine_version": "tb1-full-frame"},
            "mem_cap_bytes": None,
            "rlimit_kind": "data",
            "staging_output_dir": str(staging_output_dir),
        }

        envelope = _isolated_worker._run(payload)

        assert envelope["outcome"] == "completed"
        assert envelope["quality_metrics"]["execution"]["outputs_streamed"] is False
        assert envelope["staged_tables"] == ["t"]
        assert (staging_output_dir / "t.parquet").exists()
