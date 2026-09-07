"""Fresh-process peak-RSS probe for the widened native lane (Q3 slice 2,
docs/plans/2026-09-05-native-route-wider-types.md, acceptance test 6).

Mirrors `native_route_seam_probe.py`'s two-subcommand shape, but the fixture
carries integer/boolean/timestamp columns (all no-null, so every column
lands on an Admit cell and the preflight's SECOND read actually runs) instead
of slice 1's utf8-only columns. The point of measurement is the same: the
preflight accumulator is O(columns), not O(rows), so peak RSS through the
production entry must stay flat as row count grows even though this lane now
costs two full streaming passes instead of one.

  python native_route_wider_types_probe.py build <parquet_path> <n_rows>
  python native_route_wider_types_probe.py run <parquet_path> <target_dir>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

_WRITE_BATCH_ROWS = 50_000


def _peak_rss_kb() -> int:
    """Peak RSS from /proc VmHWM, in kB. VmHWM (not `ru_maxrss`): this script is
    spawned via subprocess fork+exec, and ru_maxrss survives the execve, so a
    child forked from a large parent carries the parent's fork-time high-water
    and over-reports when the probe runs after a heavy suite. VmHWM resets on
    execve. Matches the OOC memory sentinel (test_ooc_external_sort_memory.py)."""
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
    raise RuntimeError("VmHWM not found in /proc/self/status")


def _row_values(start: int, n: int) -> dict[str, list[object]]:
    idx = range(start, start + n)
    return {
        "pt_int": [i for i in idx],
        "pt_bool": [i % 2 == 0 for i in idx],
        "pt_ts": [i * 1000 for i in idx],
        "rd_int": [i for i in idx],
    }


def build(path: str, n_rows: int) -> None:
    """Write `n_rows` of the four-column widened fixture, batch by batch, so
    the BUILD process itself never holds the whole table resident either."""
    schema = pa.schema(
        [
            pa.field("pt_int", pa.int64()),
            pa.field("pt_bool", pa.bool_()),
            pa.field("pt_ts", pa.timestamp("ms")),
            pa.field("rd_int", pa.int64()),
        ]
    )
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
        "global_settings": {"seed": 20260905},
        "sources": {table: {"type": "file", "format": "parquet", "path": source_path}},
        "targets": {table: {"type": "file", "format": "parquet", "path": f"{target_dir}/out"}},
        "tables": [
            {
                "name": table,
                "columns": [
                    {"name": "pt_int", "strategy": "passthrough"},
                    {"name": "pt_bool", "strategy": "passthrough"},
                    {"name": "pt_ts", "strategy": "passthrough"},
                    {
                        "name": "rd_int",
                        "strategy": "redact",
                        "provider_config": {"redact_with": "REDACTED"},
                    },
                ],
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def run(parquet_path: str, target_dir: str) -> None:
    from decoy_engine.execution._pipeline import run_pipeline
    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
    from decoy_engine.profile._readers import LazySource

    table = "t"
    config = _build_config(table, parquet_path, target_dir)
    n_rows = LazySource(path=Path(parquet_path)).num_rows
    sink = ParquetTransactionalSink(Path(target_dir))

    result = run_pipeline(
        config,
        {table: LazySource(path=Path(parquet_path))},
        engine_version="native-route-wider-types-probe",
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
        run(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(f"unknown subcommand {cmd!r}; expected 'build' or 'run'")


if __name__ == "__main__":
    main()
