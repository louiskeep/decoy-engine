"""Worker entrypoint for an isolated `run_pipeline` invocation.

Pattern: ported from `scripts/fk_memory_probe.py`'s worker convention (see
`_isolated_common` module docstring for the full citation) -- a fresh
`python -m` child, JSON payload read from a temp file (never argv, per the
driver's docstring), a hard `resource.setrlimit` applied BEFORE any workload
allocation, and a JSON result envelope written to a known file in work_root
(dennis review HIGH-1; see `_isolated_common.RESULT_FILENAME`'s docstring for
why NOT stdout's last line, the probe's own `--json` convention this used to
copy verbatim).

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
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._isolated_common import (
    RESULT_FILENAME,
    apply_mem_cap,
    is_memory_failure,
    peak_rss_mb,
)
from decoy_engine.execution._pipeline import run_pipeline


def _write_envelope_file(envelope: dict[str, Any], result_path: Path) -> None:
    """Write the result envelope to `result_path` -- NOT stdout (HIGH-1).

    Any other diagnostic output the worker or a dependency writes (atexit
    handlers, BLAS/OpenMP teardown chatter, a chatty provider, a stray user
    print) is free to land on stdout without risk: the driver reads this
    file, never stdout's last line. `default=str` is the safety net for
    `quality_metrics`: it is already meant to be manifest-safe (the platform
    stamps it straight into a job's JSON manifest today), but one unexpected
    non-serializable value in that free-form dict must not turn a genuinely
    completed run into a `crashed` one just because the envelope itself
    failed to serialize.
    """
    result_path.write_text(json.dumps(envelope, default=str), encoding="utf-8")


def _load_sources(manifest: dict[str, str]) -> dict[str, pa.Table]:
    return {
        name: pq.read_table(path)  # type: ignore[no-untyped-call, unused-ignore]
        for name, path in manifest.items()
    }


def _stage_outputs(outputs: dict[str, pa.Table], staging_output_dir: str) -> list[str]:
    """Write each output table to the staging directory the driver chose.

    This IS the "child writes to a staging location" half of spec §12
    ruling 3 -- the driver never lets this directory become visible at a
    caller-facing target path until it observes a clean exit + a `completed`
    envelope (see `_isolated_run.run_pipeline_isolated`'s commit step). A
    SIGKILL between this write and process exit leaves an orphaned staging
    directory the driver discards, never a partial target.
    """
    # Peak-bias caveat for B5 (LOW-4): this write itself allocates Parquet
    # write buffers, so a peak sampled AFTER this call (see `_run` below)
    # includes the staging write's own footprint, not purely the masking/
    # generation work's peak. Left as-is for part-1 (reordering risks other
    # invariants); B5's telemetry consumer should be aware peak_rss_mb is a
    # slight overestimate of the job's own working set for this reason.
    os.makedirs(staging_output_dir, exist_ok=True)
    written = []
    for table, data in outputs.items():
        dest = f"{staging_output_dir}/{table}.parquet"
        pq.write_table(data, dest)  # type: ignore[no-untyped-call, unused-ignore]
        written.append(table)
    return written


def _stage_row_errors(row_errors: tuple[Any, ...], staging_output_dir: str) -> None:
    """Write row_errors alongside the staged outputs (MED-4).

    `row_errors.json` is a plain list of `{table, column, row_index,
    trigger, reason}` objects -- `RowErrorRecord` is already all str/int
    fields, so no richer serialization is needed. Written into the SAME
    staging directory `_stage_outputs` just created, so it rides the
    identical commit-or-discard fate as the output Parquet: read back by
    the driver before the atomic rename (or before discard when there is
    no `output_dir`), never left behind. A no-op when there are no row
    errors, so the common case adds no extra file.
    """
    if not row_errors:
        return
    payload = [
        {
            "table": r.table,
            "column": r.column,
            "row_index": r.row_index,
            "trigger": r.trigger,
            "reason": r.reason,
        }
        for r in row_errors
    ]
    path = f"{staging_output_dir}/row_errors.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


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
    _stage_row_errors(result.row_errors, payload["staging_output_dir"])
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
        sys.stderr.write(
            "usage: python -m decoy_engine.execution._isolated_worker "
            f"<payload_path>, got {argv!r}\n"
        )
        return 2

    # payload_path's own parent IS work_root by construction (the driver
    # writes payload.json at work_root's top level) -- result_path is
    # derived from it rather than passed separately, so worker and driver
    # can never disagree about where the envelope lands.
    payload_path = Path(argv[1])
    result_path = payload_path.parent / RESULT_FILENAME
    try:
        with open(payload_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        envelope = _run(payload)
    except BaseException as exc:  # payload itself unreadable/malformed
        envelope = {
            "outcome": "crashed",
            "peak_rss_mb": round(peak_rss_mb(), 1),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    _write_envelope_file(envelope, result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
