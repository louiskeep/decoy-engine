"""Fresh-process peak-RSS probe for the production single-pass native lane
(docs/plans/2026-09-04-native-route-production-seam.md, acceptance test 5).

Two subcommands, run as SEPARATE processes so building a fixture never
counts toward the measured run's peak RSS:

  python native_route_seam_probe.py build <parquet_path> <n_rows>
  python native_route_seam_probe.py run <parquet_path> <target_dir> [batch_rows]

`run` drives the PRODUCTION entry (`run_pipeline` with `native_route_enabled=
True`, `execution_mode="auto"`) over a `LazySource`-backed input and a
`ParquetTransactionalSink` output, then reports its own peak RSS from `/proc`
VmHWM (not `ru_maxrss`: this script is spawned via subprocess fork+exec, and
`ru_maxrss` survives the execve, so a child forked from a large parent carries
the parent's fork-time high-water and over-reports when the probe runs after a
heavy suite; VmHWM resets on execve). One rep per process invocation, so a
caller wanting several reps just spawns this script several times.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

_WRITE_BATCH_ROWS = 50_000


def _peak_rss_kb() -> int:
    """Peak RSS from /proc VmHWM, in kB -- see the module docstring for why not
    `ru_maxrss`. Matches the OOC memory sentinel
    (tests/perf/test_ooc_external_sort_memory.py)."""
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
    raise RuntimeError("VmHWM not found in /proc/self/status")


def _row_values(start: int, n: int) -> dict[str, list[str]]:
    idx = range(start, start + n)
    return {
        "pt": [f"passthrough_{i}" for i in idx],
        "rd": [f"secret_{i}" for i in idx],
        "tr1": [f"headvalue_{i:08d}" for i in idx],
        "tr2": [f"tailvalue_{i:08d}" for i in idx],
    }


def build(path: str, n_rows: int) -> None:
    """Write `n_rows` of the four-column utf8 fixture, batch by batch, so
    the BUILD process itself never holds the whole table resident either."""
    schema = pa.schema([pa.field(name, pa.utf8()) for name in ("pt", "rd", "tr1", "tr2")])
    writer = pq.ParquetWriter(path, schema)
    written = 0
    try:
        while written < n_rows:
            batch_n = min(_WRITE_BATCH_ROWS, n_rows - written)
            table = pa.table(_row_values(written, batch_n), schema=schema)
            writer.write_table(table)
            written += batch_n
    finally:
        writer.close()


def _build_config(table: str, source_path: str, target_dir: str) -> dict:
    from decoy_engine.config import PipelineConfig

    raw = {
        "version": 1,
        "global_settings": {"seed": 20260904},
        "sources": {table: {"type": "file", "format": "parquet", "path": source_path}},
        "targets": {table: {"type": "file", "format": "parquet", "path": f"{target_dir}/out"}},
        "tables": [
            {
                "name": table,
                "columns": [
                    {"name": "pt", "strategy": "passthrough"},
                    {"name": "rd", "strategy": "redact"},
                    {
                        "name": "tr1",
                        "strategy": "truncate",
                        "provider_config": {"length": 4, "keep": "head"},
                    },
                    {
                        "name": "tr2",
                        "strategy": "truncate",
                        "provider_config": {"length": 4, "keep": "tail"},
                    },
                ],
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def run(parquet_path: str, target_dir: str, batch_rows: int) -> None:
    from decoy_engine.execution._pipeline import run_pipeline
    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
    from decoy_engine.profile._readers import LazySource

    del (
        batch_rows
    )  # the lane's own batch size is internal (_NATIVE_BATCH_ROWS_DEFAULT); not a public knob
    table = "t"
    config = _build_config(table, parquet_path, target_dir)
    n_rows = LazySource(path=Path(parquet_path)).num_rows
    sink = ParquetTransactionalSink(Path(target_dir))

    result = run_pipeline(
        config,
        {table: LazySource(path=Path(parquet_path))},
        engine_version="native-route-seam-probe",
        native_route_enabled=True,
        execution_mode="auto",
        sink=sink,
    )
    peak_rss_kb = _peak_rss_kb()
    rec = {
        "n_rows": n_rows,
        "native_admitted": result.native_route is not None and result.native_route.admitted,
        "reroute_reason": result.native_route.reason if result.native_route is not None else None,
        "peak_rss_kb": peak_rss_kb,
    }
    print("BENCH_JSON " + json.dumps(rec))


def main() -> None:
    cmd = sys.argv[1]
    if cmd == "build":
        build(sys.argv[2], int(sys.argv[3]))
    elif cmd == "run":
        batch_rows = int(sys.argv[4]) if len(sys.argv) > 4 else 50_000
        run(sys.argv[2], sys.argv[3], batch_rows)
    else:
        raise SystemExit(f"unknown subcommand {cmd!r}; expected 'build' or 'run'")


if __name__ == "__main__":
    main()
