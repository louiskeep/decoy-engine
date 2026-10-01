#!/usr/bin/env python3
"""Stage 2b check A2 + entry point 2 (platform full-frame wrapper).

Runs a Rust-admitted job (single-table hash mask, resident-eligible local
Parquet) and a pandas-forced job through `api.jobs.v2_runner.run_v2_pipeline`
-- the platform's real full-frame wrapper (entry point 2), a thin layer
around `decoy_engine.run_pipeline` (see its own docstring). Then applies the
SAME derived-timing formula `api/jobs/v2_full_frame.py:83-91`
(`run_full_frame_branch`) uses, verbatim, rather than reimplementing it:

    _execute_ms = sum(t.elapsed_ms for t in result.timings)

`run_full_frame_branch` itself is not called directly because it also
persists `JobNodeRun` rows against a live SQLAlchemy `Job`/DB session (out of
scope for a read-focused evidence probe); `run_v2_pipeline` is the exact
engine-call boundary that formula reads from, so calling it directly and
applying the platform's own formula gives the same number
`run_full_frame_branch` would have recorded.

Records whether the Rust-admitted job's `result.timings` is empty (per
`decoy_engine/execution/_unified_slice.py:371`, `timings=()` unconditionally
on that lane) and so what `phase_timings["execute"]` becomes -- the "Rust
timings gap" the plan's check 2 names.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_LOG = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "runs.jsonl"
SCRATCH = Path("/dev/shm/audit-2026-09-30/scratch")  # noqa: S108


def _write_fixture(n: int, name: str) -> str:
    import pyarrow as pa
    import pyarrow.parquet as pq

    SCRATCH.mkdir(parents=True, exist_ok=True)
    table = pa.table({"h": pa.array([f"user{i}@example.com" for i in range(n)], type=pa.string())})
    path = SCRATCH / f"{name}.parquet"
    pq.write_table(table, path)
    return str(path)


def _run_one(cell_id: str, description: str, config: dict, ledger_ids: list[str]) -> dict:
    from api.jobs.v2_runner import run_v2_pipeline

    phase_timings: dict[str, float] = {}
    t0 = time.time()
    result = run_v2_pipeline(config, phase_timings=phase_timings)
    wall = time.time() - t0

    # Platform's OWN derived-execute formula, v2_full_frame.py:83-91, applied
    # here verbatim (not reimplemented, not approximated).
    execute_ms = sum(t.elapsed_ms for t in result.timings)
    boundary_ms = float(getattr(result, "boundary_conversion_ms", 0.0) or 0.0)
    read_ms = phase_timings.get("read", 0.0)
    run_wall_ms = wall * 1000.0
    profile_compile_ms = max(0.0, run_wall_ms - read_ms - execute_ms - boundary_ms)

    qm = result.quality_metrics
    unified_leaf = qm.get("unified_slice_activation")
    unified_activated = bool(unified_leaf and unified_leaf.get("activated"))

    return {
        "cell_id": cell_id,
        "description": description,
        "kind": "stage2b_platform_wrapper_timings",
        "ledger_ids": ledger_ids,
        "params": {
            "entry_point": "platform_full_frame_wrapper (api.jobs.v2_runner.run_v2_pipeline)"
        },
        "route_evidence": {
            "execution": qm.get("execution"),
            "unified_slice_activated": unified_activated,
            "engine_result_timings_count": len(result.timings),
            "engine_result_timings_raw": [
                {"node_id": getattr(t, "node_id", None), "elapsed_ms": t.elapsed_ms}
                for t in result.timings
            ],
        },
        "platform_phase_timings_read_ms": read_ms,
        "platform_phase_timings_execute_ms_per_v2_full_frame_formula": round(execute_ms, 1),
        "platform_phase_timings_boundary_conversion_ms": round(boundary_ms, 1),
        "platform_phase_timings_profile_compile_ms_derived": round(profile_compile_ms, 1),
        "overall_backend": "rust_companion" if unified_activated else "pandas",
        "wall_seconds": round(wall, 4),
        "row_count_in": None,
        "row_counts_out": {k: v.num_rows for k, v in result.outputs.items()},
    }


def main() -> None:
    n = 5_000
    rust_path = _write_fixture(n, "wrapper_timings_rust")
    pandas_path = _write_fixture(n, "wrapper_timings_pandas")

    rust_config = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {"t": {"type": "file", "format": "parquet", "path": rust_path}},
        "targets": {
            "t": {
                "type": "file",
                "format": "parquet",
                "path": str(SCRATCH / "wrapper_timings_rust.out.parquet"),
            }
        },
        "tables": [
            {"name": "t", "columns": [{"name": "h", "strategy": "hash", "namespace": "ns_h"}]}
        ],
    }
    # Force the pandas oracle for contrast via the same platform-recognized
    # settings.unified_slice_enabled knob run_v2_pipeline reads -- simplest
    # here is a `when`-bearing raw dict the unified admission declines
    # (B017), matching the ledger's own established decline fixture, so the
    # SAME wrapper call path is exercised for both cells.
    pandas_config = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {"t": {"type": "file", "format": "parquet", "path": pandas_path}},
        "targets": {
            "t": {
                "type": "file",
                "format": "parquet",
                "path": str(SCRATCH / "wrapper_timings_pandas.out.parquet"),
            }
        },
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "h", "strategy": "hash", "namespace": "ns_h", "when": "h == h"}
                ],
            }
        ],
    }

    records = [
        _run_one(
            "R029_platform_wrapper_rust_admitted",
            "hash-only, 5k rows, Rust-admitted, through the platform full-frame wrapper run_v2_pipeline",
            rust_config,
            ["B023", "B024", "B026", "B027", "B028", "B035", "B039"],
        ),
        _run_one(
            "R030_platform_wrapper_pandas_forced",
            "hash-only + when gate, 5k rows, pandas-forced (B017/B049 decline), through the same platform wrapper",
            pandas_config,
            ["B017"],
        ),
    ]

    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNS_LOG, "a") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
            print(
                f"{rec['cell_id']}: backend={rec['overall_backend']} "
                f"timings_count={rec['route_evidence']['engine_result_timings_count']} "
                f"execute_ms={rec['platform_phase_timings_execute_ms_per_v2_full_frame_formula']}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
