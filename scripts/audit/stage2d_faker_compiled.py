#!/usr/bin/env python3
"""Stage 2d, task 1 (reviewer blocker B1): does the production pandas Faker
strategy actually reach the compiled `derive_index_batch` kernel?

`FakerStrategyHandler.run` (src/decoy_engine/execution/_strategies/_faker.py
~line 86) calls `PoolSampler().sample(...)` for every Faker-strategy mask
column, on EVERY backend that runs the pandas adapter (full-frame legacy,
chunked, sequential -- Faker is never admitted to the unified/Rust slice,
B028). `PoolSampler.sample` -> `_derive_pool_indices`
(src/decoy_engine/generation/pool/_sampler.py ~48-152) calls the compiled
`derive_index_batch` kernel when the native companion is present, falling
back to a pure-Python reference kernel otherwise.

This script instruments that call INSIDE this one-shot subprocess (never
editing src/): it monkeypatches
`decoy_engine.generation.pool._sampler._compiled_index_kernel` with a
counting wrapper around the real kernel object's `derive_index_batch`
method, then runs one cell, and reports how many times the compiled kernel
was actually invoked. A 0 count with the companion present would mean the
sampler took the reference-kernel branch (e.g. because the source column's
Arrow type is not in the compiled kernel's admitted domain); a count > 0
is a real, run-witnessed compiled call, not a code-reading inference.

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


# ---------------------------------------------------------------------------
# Instrumentation: wraps the compiled-kernel selector inside THIS subprocess
# only. `_compiled_index_kernel()` is a module-level function looked up by
# name at call time inside `_derive_pool_indices`, so replacing the module
# attribute here is enough -- no src/ edit, no import-time patch of anything
# shipped.
# ---------------------------------------------------------------------------


def _install_compiled_call_counter() -> dict:
    import decoy_engine.generation.pool._sampler as sampler_mod

    counters = {"derive_index_batch_calls": 0, "kernel_was_none_calls": 0}
    original = sampler_mod._compiled_index_kernel
    wrapped_cache: dict[int, Any] = {}

    def counting_selector():
        kernel = original()
        if kernel is None:
            counters["kernel_was_none_calls"] += 1
            return None
        key = id(kernel)
        if key not in wrapped_cache:

            class _CountingKernelProxy:
                def __init__(self, inner):
                    self._inner = inner

                def derive_index_batch(self, *args, **kwargs):
                    counters["derive_index_batch_calls"] += 1
                    return self._inner.derive_index_batch(*args, **kwargs)

            wrapped_cache[key] = _CountingKernelProxy(kernel)
        return wrapped_cache[key]

    sampler_mod._compiled_index_kernel = counting_selector
    return counters


# ---------------------------------------------------------------------------
# Fixture builders, one per cell
# ---------------------------------------------------------------------------


def _faker_column(name: str = "nm", *, pool_size: int = 30, deterministic: bool = True) -> dict:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_first_name",
        "deterministic": deterministic,
        "namespace": "ns_faker",
        "pool_size": pool_size,
    }


def cell_r010_pooled_faker(n: int, *, deterministic: bool = True) -> tuple[dict, dict]:
    """R010's spec: single-table pooled Faker mask, 10k rows."""
    import pyarrow as pa

    col = _faker_column(deterministic=deterministic)
    src = {"nm": pa.array([f"id-{i}" for i in range(n)], type=pa.string())}
    table = pa.table(src)
    path = _write_parquet("stage2d_r010", table)
    raw = {
        "version": 1,
        "global_settings": {"seed": 20260930},
        "sources": {"t": {"type": "file", "format": "parquet", "path": path}},
        "targets": {"t": {"type": "file", "format": "parquet", "path": f"{path}.out.parquet"}},
        "tables": [{"name": "t", "columns": [col]}],
    }
    return raw, {"t": table}


def cell_r014_mix_plus_faker(n: int) -> tuple[dict, dict]:
    """R014's spec: all-native mix plus one Faker column, 10k rows."""
    import pyarrow as pa

    cols = [
        {"name": "h", "strategy": "hash", "namespace": "ns_h"},
        {"name": "r", "strategy": "redact"},
        {"name": "t", "strategy": "truncate", "provider_config": {"length": 4, "keep": "head"}},
        {"name": "p", "strategy": "passthrough"},
        _faker_column("nm"),
    ]
    src = {
        "h": pa.array([f"user{i}@example.com" for i in range(n)], type=pa.string()),
        "r": pa.array([f"5{i % 900:03d}-11-2222" for i in range(n)], type=pa.string()),
        "t": pa.array([f"4000{i % 9999:04d}" for i in range(n)], type=pa.string()),
        "p": pa.array(list(range(n)), type=pa.int64()),
        "nm": pa.array([f"id-{i}" for i in range(n)], type=pa.string()),
    }
    table = pa.table(src)
    path = _write_parquet("stage2d_r014", table)
    raw = {
        "version": 1,
        "global_settings": {"seed": 20260930},
        "sources": {"t": {"type": "file", "format": "parquet", "path": path}},
        "targets": {"t": {"type": "file", "format": "parquet", "path": f"{path}.out.parquet"}},
        "tables": [{"name": "t", "columns": cols}],
    }
    return raw, {"t": table}


def cell_chunked_faker_150k(n: int) -> tuple[dict, dict]:
    """A chunk-eligible Faker column (deterministic, namespace, explicit
    pool_size, default cardinality_mode) at 150k rows, DEFAULT routing
    (auto_chunk=True default, 100k threshold) -- production `run_pipeline`
    default, no forced kwargs."""
    import pyarrow as pa

    col = _faker_column(pool_size=200)
    src = {"nm": pa.array([f"id-{i}" for i in range(n)], type=pa.string())}
    table = pa.table(src)
    path = _write_parquet("stage2d_chunked_faker_150k", table)
    raw = {
        "version": 1,
        "global_settings": {"seed": 20260930},
        "sources": {"t": {"type": "file", "format": "parquet", "path": path}},
        "targets": {"t": {"type": "file", "format": "parquet", "path": f"{path}.out.parquet"}},
        "tables": [{"name": "t", "columns": [col]}],
    }
    return raw, {"t": table}


def cell_r023_generate_only(n: int) -> tuple[dict, dict]:
    """R023's spec: single-table generate (one Faker generate column), 10k
    rows -- generation, not masking. Section 10 of the ledger claims this
    never touches the native companion at all; this cell checks that claim
    with the SAME counter used for the masking cells, not a separate one."""
    raw = {
        "version": 1,
        "global_settings": {"seed": 20260930},
        "sources": {},
        "targets": {
            "g": {
                "type": "file",
                "format": "parquet",
                "path": str(SCRATCH / "stage2d_r023_gen.out.parquet"),
            }
        },
        "tables": [
            {
                "name": "g",
                "row_count": n,
                "generate_columns": [{"name": "fn", "type": "faker", "faker_type": "first_name"}],
            }
        ],
    }
    return raw, {}


def cell_fk_faker_sequential(n_parent: int, n_child: int) -> tuple[dict, dict]:
    """An FK tree with a Faker payload column on the child table, forced
    through the sequential route (Faker is not unified-slice- or
    OOC-eligible; sequential is the bounded route that still admits it)."""
    import pyarrow as pa

    parent_id = pa.array([f"p{i}" for i in range(n_parent)], type=pa.string())
    parent_cols = [{"name": "id", "strategy": "hash", "namespace": "ns_key"}]
    parent_src = {"id": parent_id}
    parent_table = pa.table(parent_src)

    child_parent_id = pa.array([f"p{i % n_parent}" for i in range(n_child)], type=pa.string())
    child_cols = [
        {"name": "parent_id", "strategy": "hash", "namespace": "ns_key"},
        _faker_column("payload_nm"),
    ]
    child_src = {
        "parent_id": child_parent_id,
        "payload_nm": pa.array([f"id-{i}" for i in range(n_child)], type=pa.string()),
    }
    child_table = pa.table(child_src)

    p_path = _write_parquet("stage2d_fk_faker_parent", parent_table)
    c_path = _write_parquet("stage2d_fk_faker_child", child_table)

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
    return raw, {"parent": parent_table, "child": child_table}


_CELLS = {
    "r010_pooled_faker": lambda spec: cell_r010_pooled_faker(
        spec.get("rows", 10_000), deterministic=spec.get("deterministic", True)
    ),
    "r014_mix_plus_faker": lambda spec: cell_r014_mix_plus_faker(spec.get("rows", 10_000)),
    "chunked_faker_150k": lambda spec: cell_chunked_faker_150k(spec.get("rows", 150_000)),
    "r023_generate_only": lambda spec: cell_r023_generate_only(spec.get("rows", 10_000)),
    "fk_faker_sequential": lambda spec: cell_fk_faker_sequential(*spec.get("sizes", [200, 1000])),
}


def run_cell(spec: dict) -> dict:
    from decoy_engine.execution import run_pipeline

    counters = _install_compiled_call_counter()

    kind = spec["variant"]
    raw_config, sources = _CELLS[kind](spec)
    config = _validate_config(raw_config)
    run_kwargs = dict(spec.get("run_kwargs", {}))

    t0 = time.time()
    result = run_pipeline(
        config,
        sources,
        engine_version="stage2d-probe",
        key_provider=_key_provider(),
        explain_plan=True,
        **run_kwargs,
    )
    wall = time.time() - t0

    qm = result.quality_metrics
    env_record = _env_record()

    return {
        "cell_id": spec["id"],
        "kind": "stage2d_faker_compiled",
        "variant": kind,
        "description": spec.get("description"),
        "params": {
            "rows": spec.get("rows"),
            "sizes": spec.get("sizes"),
            "run_kwargs": run_kwargs,
            "deterministic": spec.get("deterministic", True),
            "entry_point": "engine_direct",
        },
        "compiled_kernel_evidence": {
            "derive_index_batch_calls": counters["derive_index_batch_calls"],
            "kernel_selector_returned_none_calls": counters["kernel_was_none_calls"],
        },
        "route_evidence": {
            "execution": qm.get("execution"),
            "auto_chunk": qm.get("auto_chunk"),
            "unified_slice_activation": qm.get("unified_slice_activation"),
        },
        "row_counts_out": {k: v.num_rows for k, v in result.outputs.items()},
        "table_kinds": result.table_kinds,
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
