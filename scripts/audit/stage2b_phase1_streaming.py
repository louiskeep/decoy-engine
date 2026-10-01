#!/usr/bin/env python3
"""Stage 2b check A5: platform Phase 1 claim-time streaming, single table.

`settings.streaming_min_input_mb` (default 256 MiB) is lowered so a small
audit fixture qualifies for the byte-size floor. A real DB-backed claim
(queue_worker._claim_next_job) needs a fully migrated Postgres schema and a
live job row; this script instead calls the SAME functions the claim path
calls (`api.jobs._phase1_eligibility.phase1_eligibility`, then
`api.jobs.v2_runner._run_v2_pipeline_streaming`, the function
`run_claim_time_streaming_route` -> `run_v2_pipeline_streaming_multitable`
eventually reaches per-table) -- NOT a real claim. This is recorded
explicitly below, per the task's own fallback instruction.

Two sub-cells:
  1. Parquet source, allowlisted strategies (hash/redact/truncate/
     passthrough) -- expect admission, then a real streaming execution,
     recording which engine function ran per chunk and its backend.
  2. Fixed-width source, same strategies -- witnesses the Codex claim that
     Phase 1's eligibility gate admits it (no format check in
     `_table_rejections`) while the stream reader (`streams.iter_source_
     batches`) rejects it with `NotImplementedError`.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_LOG = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "runs.jsonl"
SCRATCH = Path("/dev/shm/audit-2026-09-30/scratch")  # noqa: S108


def main() -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    SCRATCH.mkdir(parents=True, exist_ok=True)
    n = 2_000

    from api.config import settings

    settings.streaming_min_input_mb = 0.0001  # ~100 bytes: any real fixture clears it

    records = []

    # -- sub-cell 1: Parquet, allowlisted strategies --
    src_table = pa.table(
        {
            "h": pa.array([f"user{i}@example.com" for i in range(n)], type=pa.string()),
            "r": pa.array([f"5{i % 900:03d}-11-2222" for i in range(n)], type=pa.string()),
        }
    )
    src_path = SCRATCH / "phase1_parquet_source.parquet"
    pq.write_table(src_table, src_path)
    input_bytes = src_path.stat().st_size

    config = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {"t": {"type": "file", "format": "parquet", "path": str(src_path)}},
        "targets": {
            "t": {
                "type": "file",
                "format": "parquet",
                "path": str(SCRATCH / "phase1_parquet_out.parquet"),
            }
        },
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "h", "strategy": "hash", "namespace": "ns_h"},
                    {"name": "r", "strategy": "redact"},
                ],
            }
        ],
    }

    from api.jobs._phase1_eligibility import StreamingPlan, phase1_eligibility

    plan_or_reasons = phase1_eligibility(config, profile_metadata={"input_size_bytes": input_bytes})
    admitted = isinstance(plan_or_reasons, StreamingPlan)

    engine_calls: list[dict] = []
    if admitted:
        import decoy_engine.execution as engine_execution

        orig_chunked = engine_execution.run_mask_pipeline_chunked

        def _tracing_chunked(*args, **kwargs):
            engine_calls.append(
                {
                    "function": "decoy_engine.execution.run_mask_pipeline_chunked",
                    "kwargs_keys": sorted(kwargs.keys()),
                }
            )
            return orig_chunked(*args, **kwargs)

        # Patch the SAME module attribute v2_runner re-imports at call time
        # (its own docstring: "from decoy_engine.execution import
        # run_mask_pipeline_chunked" happens inside the function body, so
        # patching the source module's attribute is observed).
        engine_execution.run_mask_pipeline_chunked = _tracing_chunked
        try:
            from api.jobs.v2_runner import _run_v2_pipeline_streaming

            job = SimpleNamespace(id=1)
            t0 = time.time()
            written = _run_v2_pipeline_streaming(
                job, None, config, table="t", engine_version="stage2b-probe"
            )
            wall = time.time() - t0
            outcome = {"success": True, "written": written, "wall_seconds": round(wall, 4)}
        except Exception as exc:
            outcome = {"success": False, "error_type": type(exc).__name__, "error": str(exc)}
        finally:
            engine_execution.run_mask_pipeline_chunked = orig_chunked
    else:
        outcome = {"success": False, "reason": "not admitted by phase1_eligibility"}

    records.append(
        {
            "cell_id": "R034_phase1_streaming_parquet_single_table",
            "description": (
                "single-table Parquet, allowlisted strategies (hash, redact), streaming_min_input_mb "
                "lowered to admit a small fixture; drove the SAME functions the claim path calls "
                "(phase1_eligibility then v2_runner._run_v2_pipeline_streaming), NOT a real DB-backed "
                "claim -- Postgres/queue_worker claim path not exercised for this cell"
            ),
            "kind": "stage2b_phase1_streaming",
            "ledger_ids": ["B246-B260", "B264"],
            "params": {
                "entry_point": "platform_claim_functions_direct_call (not a real claim)",
                "streaming_min_input_mb_override": settings.streaming_min_input_mb,
                "input_bytes": input_bytes,
            },
            "route_evidence": {
                "phase1_eligibility_admitted": admitted,
                "phase1_eligibility_result": (
                    list(plan_or_reasons.tables) if admitted else plan_or_reasons
                ),
                "engine_function_calls_per_chunk": engine_calls,
                "backend": "pandas (PandasExecutionAdapter, per decoy_engine/execution/_chunked.py)"
                if admitted
                else None,
            },
            "outcome": outcome,
        }
    )

    # -- sub-cell 2: fixed_width source, same allowlisted strategies --

    fw_path = SCRATCH / "phase1_fixed_width_source.txt"
    layout_columns = [
        {"name": "h", "start": 0, "width": 30, "type": "str"},
        {"name": "r", "start": 30, "width": 12, "type": "str"},
    ]
    with open(fw_path, "w") as fh:
        for i in range(n):
            h = f"user{i}@example.com".ljust(30)
            r = f"5{i % 900:03d}-11-2222".ljust(12)
            fh.write(h + r + "\n")
    fw_input_bytes = fw_path.stat().st_size

    fw_config = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {
            "t": {
                "type": "file",
                "format": "fixed_width",
                "path": str(fw_path),
                "layout": {"columns": layout_columns},
            }
        },
        "targets": {
            "t": {
                "type": "file",
                "format": "parquet",
                "path": str(SCRATCH / "phase1_fw_out.parquet"),
            }
        },
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "h", "strategy": "hash", "namespace": "ns_h"},
                    {"name": "r", "strategy": "redact"},
                ],
            }
        ],
    }

    fw_plan_or_reasons = phase1_eligibility(
        fw_config, profile_metadata={"input_size_bytes": fw_input_bytes}
    )
    fw_admitted = isinstance(fw_plan_or_reasons, StreamingPlan)

    fw_outcome: dict
    if fw_admitted:
        try:
            from api.jobs.v2_runner import _run_v2_pipeline_streaming

            job = SimpleNamespace(id=2)
            _run_v2_pipeline_streaming(
                job, None, fw_config, table="t", engine_version="stage2b-probe"
            )
            fw_outcome = {"success": True}
        except Exception as exc:
            fw_outcome = {"success": False, "error_type": type(exc).__name__, "error": str(exc)}
    else:
        fw_outcome = {"success": False, "reason": "not admitted by phase1_eligibility (unexpected)"}

    records.append(
        {
            "cell_id": "R035_phase1_streaming_fixed_width_admit_then_reject",
            "description": (
                "fixed_width source, same allowlisted strategies -- witnesses whether "
                "phase1_eligibility admits a format it never checks, then whether the "
                "stream reader (streams.iter_source_batches) rejects it downstream"
            ),
            "kind": "stage2b_phase1_streaming",
            "ledger_ids": [],
            "params": {"entry_point": "platform_claim_functions_direct_call (not a real claim)"},
            "route_evidence": {
                "phase1_eligibility_admitted": fw_admitted,
                "phase1_eligibility_result": (
                    list(fw_plan_or_reasons.tables) if fw_admitted else fw_plan_or_reasons
                ),
            },
            "outcome": fw_outcome,
        }
    )

    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNS_LOG, "a") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    for rec in records:
        print(f"{rec['cell_id']}: {rec['outcome']}", file=sys.stderr)


if __name__ == "__main__":
    main()
