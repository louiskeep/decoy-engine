#!/usr/bin/env python3
"""Stage 2d, task 2 (H1): re-run R029's exact fixture (hash-only, 5k rows,
Rust-admitted, through the platform full-frame wrapper
`api.jobs.v2_runner.run_v2_pipeline`) and capture the engine result's
`unified_slice_activation` PER-NODE evidence (`compiled_kernel_executed`),
which R029's original record (stage 2b) did not carry -- only the table-level
`unified_slice_activated: true` flag.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_RECORD = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "environment.json"
SCRATCH = Path("/dev/shm/audit-2026-09-30/scratch")  # noqa: S108


def _vmhwm_kb() -> int | None:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def _env_record() -> dict[str, Any]:
    if not ENV_RECORD.exists():
        return {}
    return json.loads(ENV_RECORD.read_text())


def _write_fixture(n: int, name: str) -> str:
    import pyarrow as pa
    import pyarrow.parquet as pq

    SCRATCH.mkdir(parents=True, exist_ok=True)
    table = pa.table({"h": pa.array([f"user{i}@example.com" for i in range(n)], type=pa.string())})
    path = SCRATCH / f"{name}.parquet"
    pq.write_table(table, path)
    return str(path)


def main() -> None:
    from api.jobs.v2_runner import run_v2_pipeline

    n = 5_000
    path = _write_fixture(n, "stage2d_platform_per_node_rust")
    config = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {"t": {"type": "file", "format": "parquet", "path": path}},
        "targets": {
            "t": {
                "type": "file",
                "format": "parquet",
                "path": str(SCRATCH / "stage2d_platform_per_node_rust.out.parquet"),
            }
        },
        "tables": [
            {"name": "t", "columns": [{"name": "h", "strategy": "hash", "namespace": "ns_h"}]}
        ],
    }

    phase_timings: dict[str, float] = {}
    t0 = time.time()
    result = run_v2_pipeline(config, phase_timings=phase_timings)
    wall = time.time() - t0

    qm = result.quality_metrics
    leaf = qm.get("unified_slice_activation")
    unified_activated = bool(leaf and leaf.get("activated"))
    node_evidence = leaf.get("nodes") if leaf else None

    env_record = _env_record()
    record = {
        "cell_id": "R082_platform_wrapper_rust_admitted_per_node",
        "description": (
            "same fixture as R029 (hash-only, 5k rows, Rust-admitted, platform "
            "full-frame wrapper run_v2_pipeline), re-run to capture the engine "
            "result's per-node unified_slice_activation evidence, which R029's "
            "original record did not carry"
        ),
        "kind": "stage2d_platform_per_node",
        "params": {
            "entry_point": "platform_full_frame_wrapper (api.jobs.v2_runner.run_v2_pipeline)"
        },
        "route_evidence": {
            "execution": qm.get("execution"),
            "unified_slice_activated": unified_activated,
            "nodes": node_evidence,
        },
        "wall_seconds": round(wall, 4),
        "peak_memory_vmhwm_kb": _vmhwm_kb(),
        "row_counts_out": {k: v.num_rows for k, v in result.outputs.items()},
        "commits": {
            "engine_commit": env_record.get("engine", {}).get("commit"),
            "platform_commit": env_record.get("platform", {}).get("commit"),
            "cli_commit": env_record.get("cli", {}).get("commit"),
        },
        "native_companion_status": env_record.get("native_companion", {}).get(
            "native_companion_status", {}
        ),
        "native_companion_sha256": env_record.get("native_companion", {}).get("module_sha256"),
    }
    print(json.dumps(record, default=str))


if __name__ == "__main__":
    main()
