"""Worker for the B6b LazySource merge benchmark (plan 2026-10-02-b6b-lazy-batch-input,
section 10). One process, one configuration, one `run_pipeline` call with a
`ParquetTransactionalSink`, the way `_isolated_worker._run` does.

`prepare --workload native|oracle --rows N --path P [--row-group-rows G]`
    Write the fixed-seed source with a bounded `ParquetWriter` loop: one deterministic
    1,048,576-row table at a time from one continuing RNG, then a remainder; `Table.nbytes` is
    accumulated, and no full table exists. `--one-group` instead writes the rows as a single row
    group in one shot (the 10M one-row-group cell). Prints a JSON line with the file size,
    accumulated `Table.nbytes`, row-group count and the preparation peak RSS, and exits 3 when
    the peak is above 3 GiB.

`run --workload W --rows N --source P --config a|b --workdir D --out J`
    Config `a` (baseline) reads the source with `pq.read_table` and passes the table; config `b`
    (candidate) passes `LazySource(P)`. Both then call `run_pipeline(..., sink=...)` and
    `_isolated_worker._finalize_outputs`. The peak counter is reset (`5` to
    `/proc/self/clear_refs`) after imports and config construction and before the read;
    `VmRSS` there is the floor, so the baseline's increment includes its read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any

SEED = 20261001
TABLE = "t"
ENGINE_VERSION = "bench-lazy"
WORKLOADS = ("native", "oracle")
GROUP_ROWS = 1_048_576
PREP_PEAK_LIMIT = 3 * 1024**3


def build_chunk(workload: str, rng: Any, rows: int) -> Any:
    import numpy
    import pyarrow as pa

    ks = rng.integers(0, 10**9, size=rows, dtype=numpy.int64).tolist()
    columns = {
        "email": [f"user{v:09d}@example.com" for v in ks],
        "secret": [f"secret-{v:09d}" for v in ks],
        "zip": [f"{v:09d}" for v in ks],
        "ref": [f"keep-{v:09d}" for v in ks],
    }
    if workload == "oracle":
        columns["tier"] = [("bronze", "silver", "gold")[v % 3] for v in ks]
    return pa.table({n: pa.array(v, pa.string()) for n, v in columns.items()})


def build_config(workload: str, source_path: str) -> dict[str, Any]:
    from decoy_engine.config import PipelineConfig

    columns: list[dict[str, Any]] = [
        {"name": "email", "strategy": "hash", "namespace": "bench_ns"},
        {"name": "secret", "strategy": "redact"},
        {"name": "zip", "strategy": "truncate", "provider_config": {"length": 3, "keep": "head"}},
        {"name": "ref", "strategy": "passthrough"},
    ]
    if workload == "oracle":
        columns.append(
            {
                "name": "tier",
                "strategy": "categorical",
                "deterministic": True,
                "namespace": "tier_ns",
                "provider_config": {
                    "categories": ["free", "pro", "team"],
                    "weights": [0.6, 0.3, 0.1],
                },
            }
        )
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {TABLE: {"type": "file", "format": "parquet", "path": source_path}},
            "tables": [{"name": TABLE, "columns": columns}],
            "targets": {TABLE: {"type": "file", "format": "parquet", "path": "/dev/null"}},
        }
    ).model_dump()


def _status(key: str) -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(key + ":"):
            return int(line.split()[1]) * 1024
    raise RuntimeError(key)


def prepare(args: argparse.Namespace) -> int:
    import numpy
    import pyarrow.parquet as pq

    rng = numpy.random.default_rng(SEED)
    nbytes = 0
    if args.one_group:
        table = build_chunk(args.workload, rng, args.rows)
        nbytes = table.nbytes
        pq.write_table(table, args.path, row_group_size=args.rows)
    else:
        writer = None
        done = 0
        try:
            while done < args.rows:
                size = min(GROUP_ROWS, args.rows - done)
                table = build_chunk(args.workload, rng, size)
                nbytes += table.nbytes
                if writer is None:
                    writer = pq.ParquetWriter(args.path, table.schema)
                writer.write_table(table, row_group_size=GROUP_ROWS)
                done += size
        finally:
            if writer is not None:
                writer.close()
    meta = pq.ParquetFile(args.path).metadata
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    print(
        json.dumps(
            {
                "path": args.path,
                "rows": args.rows,
                "file_bytes": Path(args.path).stat().st_size,
                "table_nbytes": nbytes,
                "row_groups": meta.num_row_groups,
                "prep_peak_rss_bytes": peak,
            }
        )
    )
    return 3 if peak > PREP_PEAK_LIMIT else 0


def run(args: argparse.Namespace) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from decoy_engine.execution import _isolated_worker, _probe, run_pipeline
    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
    from decoy_engine.profile._readers import LazySource

    probes: list[int] = []
    real_probe = _probe.probe_peak_bytes
    _probe.probe_peak_bytes = lambda *a, **k: (probes.append(1), real_probe(*a, **k))[1]  # type: ignore[assignment]
    config = build_config(args.workload, args.source)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    staging = workdir / "out"
    Path("/proc/self/clear_refs").write_text("5")
    floor = _status("VmRSS")
    started = time.perf_counter()
    source: Any = (
        pq.read_table(args.source) if args.config == "a" else LazySource(Path(args.source))
    )
    result = run_pipeline(
        config,
        sources={TABLE: source},
        sink=ParquetTransactionalSink(staging),
        engine_version=ENGINE_VERSION,
        native_threads=1,
    )
    staged = _isolated_worker._finalize_outputs(result, str(staging))
    wall_s = time.perf_counter() - started
    hwm = _status("VmHWM")
    metrics = result.quality_metrics
    path = staging / f"{TABLE}.parquet"
    auto = metrics.get("auto_chunk") or {}
    evidence = {
        "config": args.config,
        "workload": args.workload,
        "rows": args.rows,
        "wall_s": wall_s,
        "floor_bytes": floor,
        "hwm_bytes": hwm,
        "increment_bytes": hwm - floor,
        "lifetime_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "arrow_pool_max_bytes": pa.default_memory_pool().max_memory(),
        "staged_tables": staged,
        "outputs_streamed": metrics["execution"]["outputs_streamed"],
        "loaded_fully_in_memory": metrics["execution"]["loaded_fully_in_memory"],
        "input": auto.get("input"),
        "output": auto.get("output"),
        "native_admitted": (metrics.get("chunked_route") or {}).get("native_admitted"),
        "reroute_reason": (metrics.get("chunked_route") or {}).get("reroute_reason"),
        "probe_ran": bool(probes),
        "staged_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "staged_bytes": path.stat().st_size,
    }
    Path(args.out).write_text(json.dumps(evidence, allow_nan=False, indent=1))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="verb", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--workload", choices=WORKLOADS, required=True)
    prep.add_argument("--rows", type=int, required=True)
    prep.add_argument("--path", required=True)
    prep.add_argument("--one-group", action="store_true")
    prep.set_defaults(func=prepare)
    runner = sub.add_parser("run")
    runner.add_argument("--workload", choices=WORKLOADS, required=True)
    runner.add_argument("--rows", type=int, required=True)
    runner.add_argument("--source", required=True)
    runner.add_argument("--config", choices=("a", "b"), required=True)
    runner.add_argument("--workdir", required=True)
    runner.add_argument("--out", required=True)
    runner.set_defaults(func=run)
    args = parser.parse_args(argv)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
