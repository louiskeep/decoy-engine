#!/usr/bin/env python3
"""Stage 2b check A3: output parity through the platform output layer.

For an R013-style mixed-backend job (hash: rust_companion; redact/truncate/
passthrough: arrow_python_native) and its pandas oracle (same config,
`unified_slice_enabled=False`), writes CSV and Parquet through the
platform's real `api.jobs.v2_cloud_materialize._materialize_file_output`
(the local-file half of the platform output layer; cloud targets are
covered by the A1(b) cloud e2e scripts instead) and compares the two
routes' written files byte-for-byte and value-for-value.

Also witnesses the plan's named fixed-width-output-is-schema-rejected claim:
attempts a target with `format: "fixed_width"` and records the exact
validation error (`config/_targets.py`'s `FileTarget.format` Literal has no
`fixed_width` member).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_LOG = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "runs.jsonl"
SCRATCH = Path("/dev/shm/audit-2026-09-30/scratch")  # noqa: S108


def _mix_source(n: int):
    import pyarrow as pa

    return {
        "h": pa.array([f"user{i}@example.com" for i in range(n)], type=pa.string()),
        "r": pa.array([f"5{i % 900:03d}-11-2222" for i in range(n)], type=pa.string()),
        "t": pa.array([f"4000{i % 9999:04d}" for i in range(n)], type=pa.string()),
        "p": pa.array(list(range(n)), type=pa.int64()),
    }


def _mix_columns():
    return [
        {"name": "h", "strategy": "hash", "namespace": "ns_h"},
        {"name": "r", "strategy": "redact"},
        {"name": "t", "strategy": "truncate", "provider_config": {"length": 4, "keep": "head"}},
        {"name": "p", "strategy": "passthrough"},
    ]


def main() -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from decoy_engine.config import PipelineConfig
    from decoy_engine.execution import run_pipeline
    from decoy_engine.keyprovider import SecretKeyProvider

    SCRATCH.mkdir(parents=True, exist_ok=True)
    n = 5_000
    src_table = pa.table(_mix_source(n))
    src_path = SCRATCH / "output_layer_mix.parquet"
    pq.write_table(src_table, src_path)
    reloaded = pq.read_table(src_path)

    raw_config = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {"t": {"type": "file", "format": "parquet", "path": str(src_path)}},
        "targets": {"t": {"type": "file", "format": "parquet", "path": str(SCRATCH / "output_layer_mix.out.parquet")}},
        "tables": [{"name": "t", "columns": _mix_columns()}],
    }
    config = PipelineConfig.model_validate(raw_config).model_dump()
    key_provider = SecretKeyProvider(secret=bytes(range(32)), key_version="v1")

    rust_result = run_pipeline(
        config, {"t": reloaded}, engine_version="stage2b-probe", key_provider=key_provider, explain_plan=True
    )
    pandas_result = run_pipeline(
        config,
        {"t": reloaded},
        engine_version="stage2b-probe",
        key_provider=key_provider,
        explain_plan=True,
        unified_slice_enabled=False,
    )

    rust_qm = rust_result.quality_metrics
    rust_leaf = rust_qm.get("unified_slice_activation")
    rust_nodes = rust_leaf.get("nodes") if rust_leaf else {}

    from api.jobs.v2_cloud_materialize import _materialize_file_output

    records = []
    for fmt, suffix in (("parquet", ".parquet"), ("csv", ".csv")):
        rust_path = SCRATCH / f"output_layer_rust{suffix}"
        pandas_path = SCRATCH / f"output_layer_pandas{suffix}"
        _materialize_file_output(rust_result.outputs["t"], {"type": "file", "path": str(rust_path)})
        _materialize_file_output(pandas_result.outputs["t"], {"type": "file", "path": str(pandas_path)})

        rust_bytes = rust_path.read_bytes()
        pandas_bytes = pandas_path.read_bytes()
        byte_identical = rust_bytes == pandas_bytes

        if fmt == "parquet":
            rust_readback = pq.read_table(rust_path)
            pandas_readback = pq.read_table(pandas_path)
        else:
            import pyarrow.csv as pa_csv

            rust_readback = pa_csv.read_csv(rust_path)
            pandas_readback = pa_csv.read_csv(pandas_path)

        value_identical = (
            rust_readback.column_names == pandas_readback.column_names
            and rust_readback.num_rows == pandas_readback.num_rows
            and all(
                rust_readback.column(c).to_pylist() == pandas_readback.column(c).to_pylist()
                for c in rust_readback.column_names
            )
        )

        records.append(
            {
                "cell_id": f"R031_output_layer_{fmt}_rust_vs_pandas",
                "description": (
                    f"mixed-backend (rust+arrow_python_native) mix job output written as {fmt} "
                    f"via the platform's _materialize_file_output, compared with the pandas-oracle "
                    f"route's own {fmt} output through the same function"
                ),
                "kind": "stage2b_output_layer",
                "ledger_ids": [],
                "params": {
                    "entry_point": "platform_output_layer (api.jobs.v2_cloud_materialize._materialize_file_output)",
                    "format": fmt,
                    "rust_backend_nodes": rust_nodes,
                },
                "route_evidence": {
                    "rust_route_backend": "mixed (hash: rust_companion; redact/truncate/passthrough: arrow_python_native)",
                    "pandas_route_backend": "pandas",
                },
                "byte_identical": byte_identical,
                "value_identical": value_identical,
                "rust_output_size_bytes": len(rust_bytes),
                "pandas_output_size_bytes": len(pandas_bytes),
                "row_count_in": n,
                "row_counts_out": {"rust": rust_readback.num_rows, "pandas": pandas_readback.num_rows},
            }
        )

    # Fixed-width output: schema-rejected. Show the exact validation error.
    fw_config = dict(raw_config)
    fw_config["targets"] = {"t": {"type": "file", "format": "fixed_width", "path": str(SCRATCH / "should_not_exist.fw")}}
    try:
        PipelineConfig.model_validate(fw_config)
        fw_result = {"rejected": False, "error": None}
    except Exception as exc:  # noqa: BLE001 - the error text is the evidence
        fw_result = {"rejected": True, "error_type": type(exc).__name__, "error": str(exc)}

    records.append(
        {
            "cell_id": "R032_output_layer_fixed_width_target_rejected",
            "description": "target format=fixed_width is schema-rejected (config/_targets.py FileTarget.format has no fixed_width member)",
            "kind": "stage2b_output_layer",
            "ledger_ids": [],
            "params": {"entry_point": "decoy_engine.config.PipelineConfig.model_validate"},
            "route_evidence": fw_result,
        }
    )

    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNS_LOG, "a") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    for rec in records:
        if "byte_identical" in rec:
            print(
                f"{rec['cell_id']}: byte_identical={rec['byte_identical']} value_identical={rec['value_identical']}",
                file=sys.stderr,
            )
        else:
            print(f"{rec['cell_id']}: rejected={rec['route_evidence']['rejected']}", file=sys.stderr)


if __name__ == "__main__":
    main()
