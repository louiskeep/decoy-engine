#!/usr/bin/env python3
"""Stage 2d, task 3 (H4): call
`decoy_engine.execution.native.run_native_or_oracle_chunked` DIRECTLY, the
way no production caller does today (stage 2a finding 2, ledger section 9).

Two shapes, 1,000,000 rows each, single table, no relationships:
  (a) hash + redact + truncate + passthrough (every strategy admitted by the
      native chunked dispatch, B158-B163).
  (b) (a) plus one categorical column -- categorical has a compiled kernel on
      the FULL-FRAME unified slice, but `CHUNKED_ROUTE_VETOED_STRATEGIES`
      (B161) vetoes it on THIS dispatch specifically (eager per-chunk emit
      cannot resolve its data-dependent output type), so the whole table is
      expected to reroute to the oracle.

Each shape is run at `native_threads=1` and `native_threads=4`. Records, per
cell: native_admitted, reroute_reason, per-node route/evidence
(`compiled_kernel_executed`, `kernel_calls`), byte-parity against the pandas
oracle (`run_mask_pipeline_chunked` called directly on a fresh copy of the
same chunks), wall time, and this process's own VmHWM.

One cell per process (four cells total: a/thread1, a/thread4, b/thread1,
b/thread4), matching the stage 2a/2b/2c probe convention.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_RECORD = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "environment.json"


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


def _build_source(n: int, *, with_categorical: bool, with_faker: bool = False):
    import pyarrow as pa

    cols: dict[str, Any] = {
        "h": pa.array([f"user{i}@example.com" for i in range(n)], type=pa.string()),
        "r": pa.array([f"5{i % 900:03d}-11-2222" for i in range(n)], type=pa.string()),
        "t": pa.array([f"4000{i % 9999:04d}" for i in range(n)], type=pa.string()),
        "p": pa.array(list(range(n)), type=pa.int64()),
    }
    if with_categorical:
        cats = ["red", "green", "blue"]
        cols["c"] = pa.array([cats[i % 3] for i in range(n)], type=pa.string())
    if with_faker:
        cols["nm"] = pa.array([f"id-{i}" for i in range(n)], type=pa.string())
    return pa.table(cols)


def _build_config(*, with_categorical: bool, with_faker: bool = False) -> dict:
    columns: list[dict[str, Any]] = [
        {"name": "h", "strategy": "hash", "namespace": "ns_h"},
        {"name": "r", "strategy": "redact"},
        {"name": "t", "strategy": "truncate", "provider_config": {"length": 4, "keep": "head"}},
        {"name": "p", "strategy": "passthrough"},
    ]
    if with_categorical:
        columns.append(
            {
                "name": "c",
                "strategy": "categorical",
                "namespace": "ns_c",
                "deterministic": True,
                "provider_config": {"categories": ["red", "green", "blue"]},
            }
        )
    if with_faker:
        columns.append(
            {
                "name": "nm",
                "strategy": "faker",
                "provider": "person_first_name",
                "deterministic": True,
                "namespace": "ns_faker",
                "pool_size": 200,
            }
        )
    raw = {
        "version": 1,
        "global_settings": {"seed": 20260930},
        "sources": {"w": {"type": "file", "format": "parquet", "path": "/dev/null"}},
        "targets": {"w": {"type": "file", "format": "parquet", "path": "/dev/null.out"}},
        "tables": [{"name": "w", "columns": columns}],
    }
    return _validate_config(raw)


def _chunks(table, chunk_rows: int):
    for start in range(0, table.num_rows, chunk_rows):
        yield table.slice(start, chunk_rows)


def _concat(chunks: list) -> Any:
    import pyarrow as pa

    return pa.concat_tables(list(chunks), promote_options="none")


def _compare(native_table, oracle_table) -> dict:
    if native_table.column_names != oracle_table.column_names:
        return {"match": False, "reason": "column_names differ"}
    if native_table.num_rows != oracle_table.num_rows:
        return {"match": False, "reason": "row count differs"}
    schema_diffs = []
    value_diffs = []
    for name in native_table.column_names:
        nt = native_table.schema.field(name).type
        ot = oracle_table.schema.field(name).type
        if nt != ot:
            schema_diffs.append(f"{name}: native={nt} oracle={ot}")
            continue
        if not native_table.column(name).equals(oracle_table.column(name)):
            value_diffs.append(name)
    return {
        "match": not schema_diffs and not value_diffs,
        "schema_diffs": schema_diffs,
        "value_diffs": value_diffs,
    }


def run_cell(spec: dict) -> dict:
    from decoy_engine.execution._chunked import run_mask_pipeline_chunked
    from decoy_engine.execution.native import run_native_or_oracle_chunked

    n = spec.get("rows", 1_000_000)
    with_categorical = spec["with_categorical"]
    with_faker = spec.get("with_faker", False)
    native_threads = spec["native_threads"]
    chunk_rows = spec.get("chunk_rows", 200_000)

    source = _build_source(n, with_categorical=with_categorical, with_faker=with_faker)
    config = _build_config(with_categorical=with_categorical, with_faker=with_faker)

    evidence_sink: list = []
    t0 = time.time()
    native_iter = run_native_or_oracle_chunked(
        config,
        _chunks(source, chunk_rows),
        table="w",
        engine_version="stage2d-dispatch-probe",
        key_provider=_key_provider(),
        route_evidence_sink=evidence_sink,
        native_threads=native_threads,
    )
    native_chunks = list(native_iter)
    native_wall = time.time() - t0
    native_table = _concat(native_chunks)

    t1 = time.time()
    oracle_chunks = list(
        run_mask_pipeline_chunked(
            config,
            _chunks(source, chunk_rows),
            table="w",
            engine_version="stage2d-dispatch-probe",
            key_provider=_key_provider(),
        )
    )
    oracle_wall = time.time() - t1
    oracle_table = _concat(oracle_chunks)

    evidence = evidence_sink[0] if evidence_sink else None
    evidence_dict = asdict(evidence) if evidence is not None else None
    if evidence_dict is not None:
        evidence_dict["node_routes"] = [
            {"column": nr.column, "strategy": nr.strategy, "route": nr.route}
            for nr in evidence.node_routes
        ]

    parity = _compare(native_table, oracle_table)

    env_record = _env_record()
    return {
        "cell_id": spec["id"],
        "kind": "stage2d_native_dispatch",
        "description": spec.get("description"),
        "params": {
            "rows": n,
            "with_categorical": with_categorical,
            "with_faker": with_faker,
            "native_threads": native_threads,
            "chunk_rows": chunk_rows,
            "entry_point": "direct_call:decoy_engine.execution.native.run_native_or_oracle_chunked",
        },
        "route_evidence": evidence_dict,
        "native_wall_seconds": round(native_wall, 4),
        "oracle_wall_seconds": round(oracle_wall, 4),
        "row_count_native_out": native_table.num_rows,
        "row_count_oracle_out": oracle_table.num_rows,
        "byte_parity_vs_pandas_oracle": parity,
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
