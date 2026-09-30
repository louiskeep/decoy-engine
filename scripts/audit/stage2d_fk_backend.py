#!/usr/bin/env python3
"""Stage 2d, task 5 (M6): which adapter/kernel functions actually run the
mask step for a sequential FK job and for an out-of-core FK job.

Instruments, inside this one-shot subprocess only (never editing src/):
  - `PandasExecutionAdapter.run` call count (sequential route: patched on the
    class itself, so every caller that constructs an instance is counted
    regardless of which module imported the class).
  - `out_of_core/_mask.py`'s `mask_batch` / `mask_column` / `mask_table`
    call counts, patched on each importing module's own bound name (`from
    ... import mask_batch` binds a separate reference in the importing
    module's namespace, so the source module's attribute alone is not
    enough) -- `_runner.py` and `_stream_driver.py` for `mask_batch`,
    `_batch_join.py` / `_relation.py` / `_stream_join.py` for `mask_column`,
    `_emit.py` for `mask_table`.

One cell per process, matching the stage 2a/2b/2c probe convention.
"""

from __future__ import annotations

import json
import sys
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


def _key_provider():
    from decoy_engine.keyprovider import SecretKeyProvider

    return SecretKeyProvider(secret=bytes(range(32)), key_version="v1")


def _validate_config(raw: dict) -> dict:
    from decoy_engine.config import PipelineConfig

    return PipelineConfig.model_validate(raw).model_dump()


def _write_parquet(name: str, table) -> str:
    import pyarrow.parquet as pq

    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / f"{name}.parquet"
    pq.write_table(table, path)
    return str(path)


def _install_counters() -> dict[str, int]:
    counts: dict[str, int] = {
        "pandas_execution_adapter_run": 0,
        "pandas_execution_adapter_dispatch_mask_node": 0,
        "ooc_mask_batch_runner": 0,
        "ooc_mask_batch_stream_driver": 0,
        "ooc_mask_column_runner": 0,
        "ooc_mask_column_batch_join": 0,
        "ooc_mask_column_relation": 0,
        "ooc_mask_column_stream_join": 0,
        "ooc_mask_table_emit": 0,
        "ooc_hash_array_relation": 0,
        "ooc_hash_array_mask": 0,
    }

    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

    original_run = PandasExecutionAdapter.run

    def counting_run(self, *args, **kwargs):
        counts["pandas_execution_adapter_run"] += 1
        return original_run(self, *args, **kwargs)

    PandasExecutionAdapter.run = counting_run

    # `run_sequential` (execution/_sequential.py) does not call `.run()` at
    # all -- it calls the adapter's own per-node dispatch helper directly
    # (`adapter._dispatch_mask_node(...)`), the same helper `.run()` uses
    # internally for a full-frame job. Counting both makes that finding
    # visible in the record instead of a silent 0 for `.run()`.
    original_dispatch = PandasExecutionAdapter._dispatch_mask_node

    def counting_dispatch(self, *args, **kwargs):
        counts["pandas_execution_adapter_dispatch_mask_node"] += 1
        return original_dispatch(self, *args, **kwargs)

    PandasExecutionAdapter._dispatch_mask_node = counting_dispatch

    def _wrap(mod, name: str, key: str) -> None:
        original = getattr(mod, name)

        def wrapper(*args, **kwargs):
            counts[key] += 1
            return original(*args, **kwargs)

        setattr(mod, name, wrapper)

    from decoy_engine.execution.out_of_core import (
        _batch_join,
        _emit,
        _relation,
        _runner,
        _stream_driver,
        _stream_join,
    )

    _wrap(_runner, "mask_batch", "ooc_mask_batch_runner")
    _wrap(_runner, "mask_column", "ooc_mask_column_runner")
    _wrap(_stream_driver, "mask_batch", "ooc_mask_batch_stream_driver")
    _wrap(_batch_join, "mask_column", "ooc_mask_column_batch_join")
    _wrap(_relation, "mask_column", "ooc_mask_column_relation")
    _wrap(_stream_join, "mask_column", "ooc_mask_column_stream_join")
    _wrap(_emit, "mask_table", "ooc_mask_table_emit")

    # `_relation.py` dispatches a `hash`-strategy FK PARENT key straight to
    # `decoy_engine.kernel.hash_array`, bypassing `mask_column` entirely (its
    # own comment: "Dispatched through this module's own hash_array binding
    # so the hash path's per-batch residency stays observable here").
    # `_mask.py`'s `mask_column` ALSO calls the same `hash_array` internally
    # for a payload hash column. Both are the real leaf-level hash kernel for
    # every OOC hash column, key or payload; counting both bound names shows
    # which one(s) actually ran, not just that "hash happened somewhere."
    from decoy_engine.execution.out_of_core import _mask

    _wrap(_relation, "hash_array", "ooc_hash_array_relation")
    _wrap(_mask, "hash_array", "ooc_hash_array_mask")

    return counts


# ---------------------------------------------------------------------------
# FK tree fixture, all-OOC-compatible payload (drops group_key so a forced
# out_of_core run actually succeeds instead of declining at the compat gate;
# same shape stage 2c's build_fk_tree_ooc_compatible used).
# ---------------------------------------------------------------------------


def _build_fk_tree(n_parent: int, n_child: int) -> tuple[dict, dict]:
    import pyarrow as pa

    parent_id = pa.array([f"p{i}" for i in range(n_parent)], type=pa.string())
    parent_cols = [
        {"name": "id", "strategy": "hash", "namespace": "ns_key"},
        {"name": "par_h", "strategy": "hash", "namespace": "ns_par"},
        {"name": "par_r", "strategy": "redact"},
        {"name": "par_t", "strategy": "truncate", "provider_config": {"length": 4}},
        {"name": "par_p", "strategy": "passthrough"},
    ]
    parent_src = {
        "id": parent_id,
        "par_h": pa.array([f"a{i}@ex.com" for i in range(n_parent)], type=pa.string()),
        "par_r": pa.array([f"secret{i}" for i in range(n_parent)], type=pa.string()),
        "par_t": pa.array([f"card{i:08d}" for i in range(n_parent)], type=pa.string()),
        "par_p": pa.array(list(range(n_parent)), type=pa.int64()),
    }

    child_parent_id = pa.array([f"p{i % n_parent}" for i in range(n_child)], type=pa.string())
    child_cols = [
        {"name": "parent_id", "strategy": "hash", "namespace": "ns_key"},
        {"name": "chi_h", "strategy": "hash", "namespace": "ns_chi"},
        {"name": "chi_r", "strategy": "redact"},
        {"name": "chi_t", "strategy": "truncate", "provider_config": {"length": 4}},
        {"name": "chi_p", "strategy": "passthrough"},
    ]
    child_src = {
        "parent_id": child_parent_id,
        "chi_h": pa.array([f"b{i}@ex.com" for i in range(n_child)], type=pa.string()),
        "chi_r": pa.array([f"csecret{i}" for i in range(n_child)], type=pa.string()),
        "chi_t": pa.array([f"ccrd{i:08d}" for i in range(n_child)], type=pa.string()),
        "chi_p": pa.array(list(range(n_child)), type=pa.int64()),
    }

    p_path = _write_parquet("stage2d_fk_backend_parent", pa.table(parent_src))
    c_path = _write_parquet("stage2d_fk_backend_child", pa.table(child_src))

    raw = {
        "version": 1,
        "global_settings": {"seed": 20260930},
        "sources": {
            "parent": {"type": "file", "format": "parquet", "path": p_path},
            "child": {"type": "file", "format": "parquet", "path": c_path},
        },
        "targets": {
            "parent": {"type": "file", "format": "parquet", "path": f"{p_path}.out.parquet"},
            "child": {"type": "file", "format": "parquet", "path": f"{c_path}.out.parquet"},
        },
        "tables": [
            {"name": "parent", "columns": parent_cols},
            {"name": "child", "columns": child_cols},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_key",
            }
        ],
    }
    return raw, {"parent": pa.table(parent_src), "child": pa.table(child_src)}


def run_cell(spec: dict) -> dict:
    counts = _install_counters()

    from decoy_engine.execution import run_pipeline

    n_parent, n_child = spec.get("sizes", [2_000, 8_000])
    raw_config, sources = _build_fk_tree(n_parent, n_child)
    config = _validate_config(raw_config)
    run_kwargs = dict(spec.get("run_kwargs", {}))

    t0 = time.time()
    result = run_pipeline(
        config,
        sources,
        engine_version="stage2d-fk-backend-probe",
        key_provider=_key_provider(),
        explain_plan=True,
        **run_kwargs,
    )
    wall = time.time() - t0

    qm = result.quality_metrics
    env_record = _env_record()

    return {
        "cell_id": spec["id"],
        "kind": "stage2d_fk_backend",
        "description": spec.get("description"),
        "params": {
            "sizes": [n_parent, n_child],
            "run_kwargs": run_kwargs,
            "entry_point": "engine_direct",
        },
        "route_evidence": {
            "execution": qm.get("execution"),
            "out_of_core": qm.get("out_of_core"),
            "sequential": qm.get("sequential"),
        },
        "adapter_call_counts": counts,
        "row_counts_out": {k: v.num_rows for k, v in result.outputs.items()},
        "wall_seconds": round(wall, 4),
        "peak_memory_vmhwm_kb": _vmhwm_kb(),
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


def main() -> None:
    spec = json.loads(sys.argv[1]) if len(sys.argv) > 1 else json.loads(sys.stdin.read())
    record = run_cell(spec)
    print(json.dumps(record, default=str))


if __name__ == "__main__":
    main()
