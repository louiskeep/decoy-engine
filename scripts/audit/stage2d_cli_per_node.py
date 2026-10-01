#!/usr/bin/env python3
"""Stage 2d, task 2 (H1): re-run the R036/R037 CLI cells (default mode and
`--native`, hash-only 5k rows) and capture PER-NODE evidence straight from the
engine's own `ExecutionResult.quality_metrics["unified_slice_activation"]`,
not the CLI's own `classify_route` label (`_native_gate.py`'s `state`/`label`
fields, which only report "was at least one node native", B200).

Runs the `decoy` CLI's `run` Typer command IN-PROCESS via
`typer.testing.CliRunner` (this is still "the CLI entry point": the same
`run()` function a real `decoy run` invocation calls, not a hand-rolled
`run_pipeline` call). Instruments `decoy_engine.run_pipeline` inside this
one-shot subprocess only: `run.py` does `from decoy_engine import
run_pipeline` INSIDE the function body on every call, so patching the
`decoy_engine` package attribute before invoking the CLI command is enough to
capture the real return value the CLI itself receives and then discards
after writing output.

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


def _write_fixture(n: int) -> str:
    import pyarrow as pa
    import pyarrow.parquet as pq

    SCRATCH.mkdir(parents=True, exist_ok=True)
    table = pa.table({"h": pa.array([f"user{i}@example.com" for i in range(n)], type=pa.string())})
    path = SCRATCH / "stage2d_cli_hash_5k.parquet"
    pq.write_table(table, path)
    return str(path)


def _write_config(source_path: str, target_path: str) -> str:
    import yaml

    cfg = {
        "version": 1,
        "global_settings": {"seed": 20260930},
        "sources": {"t": {"type": "file", "format": "parquet", "path": source_path}},
        "targets": {"t": {"type": "file", "format": "parquet", "path": target_path}},
        "tables": [
            {"name": "t", "columns": [{"name": "h", "strategy": "hash", "namespace": "ns_h"}]}
        ],
    }
    config_path = SCRATCH / "stage2d_cli_hash_5k.yaml"
    config_path.write_text(yaml.safe_dump(cfg))
    return str(config_path)


def _install_result_capture() -> list:
    import decoy_engine

    captured: list = []
    original = decoy_engine.run_pipeline

    def capturing_run_pipeline(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.append(result)
        return result

    decoy_engine.run_pipeline = capturing_run_pipeline
    return captured


def run_cell(spec: dict) -> dict:
    from decoy.__main__ import app
    from typer.testing import CliRunner

    captured = _install_result_capture()

    n = spec.get("rows", 5_000)
    source_path = _write_fixture(n)
    target_path = str(SCRATCH / f"{spec['id']}.out.parquet")
    config_path = _write_config(source_path, target_path)

    cli_args = ["run", config_path, "--json", *spec.get("cli_flags", [])]

    runner = CliRunner()
    t0 = time.time()
    result = runner.invoke(app, cli_args)
    wall = time.time() - t0

    engine_result = captured[0] if captured else None
    node_evidence = None
    unified_activated = None
    if engine_result is not None:
        qm = engine_result.quality_metrics
        leaf = qm.get("unified_slice_activation")
        unified_activated = bool(leaf and leaf.get("activated"))
        node_evidence = leaf.get("nodes") if leaf else None

    cli_stdout_json = None
    try:
        cli_stdout_json = json.loads(result.stdout) if result.stdout else None
    except (json.JSONDecodeError, ValueError):
        cli_stdout_json = None

    env_record = _env_record()
    return {
        "cell_id": spec["id"],
        "kind": "stage2d_cli_per_node",
        "description": spec.get("description"),
        "params": {
            "rows": n,
            "cli_flags": spec.get("cli_flags", []),
            "entry_point": "cli (in-process typer.testing.CliRunner, decoy.__main__.app)",
        },
        "cli_exit_code": result.exit_code,
        "cli_reported_native_route": (cli_stdout_json or {}).get("native_route"),
        "engine_result_captured": engine_result is not None,
        "engine_per_node_evidence": {
            "unified_slice_activated": unified_activated,
            "nodes": node_evidence,
        },
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
