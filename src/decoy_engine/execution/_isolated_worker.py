"""Worker entrypoint for an isolated `run_pipeline` invocation.

Pattern: ported from `scripts/fk_memory_probe.py`'s worker convention (see
`_isolated_common` module docstring for the full citation) -- a fresh
`python -m` child, JSON payload read from a temp file (never argv, per the
driver's docstring), a hard `resource.setrlimit` applied BEFORE any workload
allocation, and a single JSON result envelope printed as the last stdout
line so the driver can `.strip().splitlines()[-1]` it exactly as the probe's
`_sweep`/`_capability` callers do.

Invoked as `[sys.executable, "-m", "decoy_engine.execution._isolated_worker",
<payload_path>]` by `_isolated_run.run_pipeline_isolated`; not a public
entrypoint (no `if __main__` CLI ergonomics beyond that one positional arg).

Heavy imports happen at module load time, BEFORE `main()` applies the memory
cap -- matching fk_memory_probe's own ordering (imports are the interpreter's
baseline footprint, not "workload allocation"; capping before them would make
every capped run fail on import instead of on the job it is meant to bound).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._isolated_common import (
    apply_mem_cap,
    is_memory_failure,
    peak_rss_mb,
)
from decoy_engine.execution._pipeline import run_pipeline


def _write_envelope(envelope: dict[str, Any]) -> None:
    """Print the result envelope as the SOLE, LAST stdout line.

    Any other diagnostic output the worker or a dependency writes belongs on
    stderr; the driver trusts stdout's last line to be exactly this JSON
    object (matching fk_memory_probe's `--json` convention). `default=str`
    is the safety net for `quality_metrics`: it is already meant to be
    manifest-safe (the platform stamps it straight into a job's JSON
    manifest today), but one unexpected non-serializable value in that
    free-form dict must not turn a genuinely completed run into a `crashed`
    one just because the envelope itself failed to print.
    """
    print(json.dumps(envelope, default=str))


def _load_sources(manifest: dict[str, str]) -> dict[str, pa.Table]:
    return {name: pq.read_table(path) for name, path in manifest.items()}


def _stage_outputs(outputs: dict[str, pa.Table], staging_output_dir: str) -> list[str]:
    """Write each output table to the staging directory the driver chose.

    This IS the "child writes to a staging location" half of spec §12
    ruling 3 -- the driver never lets this directory become visible at a
    caller-facing target path until it observes a clean exit + a `completed`
    envelope (see `_isolated_run.run_pipeline_isolated`'s commit step). A
    SIGKILL between this write and process exit leaves an orphaned staging
    directory the driver discards, never a partial target.
    """
    os.makedirs(staging_output_dir, exist_ok=True)
    written = []
    for table, data in outputs.items():
        dest = f"{staging_output_dir}/{table}.parquet"
        pq.write_table(data, dest)
        written.append(table)
    return written


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    mem_cap_bytes = payload.get("mem_cap_bytes")
    rlimit_kind = payload.get("rlimit_kind", "data")
    if mem_cap_bytes is not None:
        # Must run before ANY workload allocation -- source loading and
        # run_pipeline both count. Imports above already happened at module
        # load, which is the intended split (see module docstring).
        apply_mem_cap(mem_cap_bytes, rlimit_kind)

    try:
        sources = _load_sources(payload.get("sources") or {})
        result = run_pipeline(payload["config"], sources, **payload["kwargs"])
    except BaseException as exc:
        outcome = "oom_killed" if is_memory_failure(exc) else "crashed"
        return {
            "outcome": outcome,
            "peak_rss_mb": round(peak_rss_mb(), 1),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }

    staged_tables = _stage_outputs(result.outputs, payload["staging_output_dir"])
    return {
        "outcome": "completed",
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "staging_output_dir": payload["staging_output_dir"],
        "staged_tables": staged_tables,
        "quality_metrics": dict(result.quality_metrics),
        "table_kinds": dict(result.table_kinds),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _write_envelope(
            {
                "outcome": "crashed",
                "peak_rss_mb": round(peak_rss_mb(), 1),
                "error": f"usage: python -m decoy_engine.execution._isolated_worker <payload_path>, got {argv!r}",
            }
        )
        return 0

    payload_path = argv[1]
    with open(payload_path, encoding="utf-8") as fh:
        payload = json.load(fh)

    envelope = _run(payload)
    _write_envelope(envelope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
