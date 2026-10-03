"""Worker for the B6a incremental-sink merge benchmark (plan
2026-10-02-b6a-incremental-output-sink, Design 10). One process, one configuration, one
`run_pipeline` call with a `ParquetTransactionalSink`, the way `_isolated_worker._run` does.

`prepare --workload native|oracle --rows N --path P`
    Write the fixed-seed source as Parquet (`run_pipeline` profiles the source from its
    config's file path). Not measured.

`run --workload W --rows N --source P --mode resident|streamed --workdir D --out J`
    Read the source with `pq.read_table`, reset the kernel's peak-RSS counter by writing
    `5` to `/proc/self/clear_refs`, record `VmRSS` as the floor, run `run_pipeline(config,
    sources, sink=ParquetTransactionalSink(D/out), ...)` and `_isolated_worker._finalize_outputs`,
    then record `VmHWM`. The run increment is `VmHWM - floor`. Wall time spans `run_pipeline`
    entry to `_finalize_outputs` return. The staged file's SHA-256 is taken after the clock stops.

Workload: B2's section 11 shape. `rng = numpy.random.default_rng(20261001)`, `k =
rng.integers(0, 10**9, size=rows)`; four null-free `string` columns built from `k`: `email`
(hash, namespace `bench_ns`), `secret` (redact), `zip` (truncate, length 3), `ref`
(passthrough). `oracle` adds the string column `tier` (`bronze`/`silver`/`gold` by `k % 3`)
masked by a deterministic `categorical` column, which B1 cannot run natively, so the whole
table takes the oracle route. Global seed 42, `native_threads=1`.
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
ENGINE_VERSION = "bench-sink"
WORKLOADS = ("native", "oracle")


def build_table(workload: str, rows: int) -> Any:
    import numpy
    import pyarrow as pa

    rng = numpy.random.default_rng(SEED)
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
    import pyarrow.parquet as pq

    pq.write_table(build_table(args.workload, args.rows), args.path)
    return 0


def run(args: argparse.Namespace) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from decoy_engine.execution import _isolated_worker, run_pipeline
    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink

    source = pq.read_table(args.source)
    config = build_config(args.workload, args.source)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    staging = workdir / "out"
    extra: dict[str, Any] = {"native_threads": 1}
    if args.mode == "resident":
        extra["stream_chunked_output"] = False
    Path("/proc/self/clear_refs").write_text("5")
    floor = _status("VmRSS")
    started = time.perf_counter()
    result = run_pipeline(
        config,
        sources={TABLE: source},
        sink=ParquetTransactionalSink(staging),
        engine_version=ENGINE_VERSION,
        **extra,
    )
    output_nbytes = result.outputs[TABLE].nbytes if TABLE in result.outputs else None
    staged = _isolated_worker._finalize_outputs(result, str(staging))
    wall_s = time.perf_counter() - started
    hwm = _status("VmHWM")
    metrics = result.quality_metrics
    path = staging / f"{TABLE}.parquet"
    evidence = {
        "mode": args.mode,
        "workload": args.workload,
        "rows": args.rows,
        "wall_s": wall_s,
        "floor_bytes": floor,
        "hwm_bytes": hwm,
        "increment_bytes": hwm - floor,
        "lifetime_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "arrow_pool_max_bytes": pa.default_memory_pool().max_memory(),
        "source_nbytes": source.nbytes,
        "output_nbytes_resident": output_nbytes,
        "staged_tables": staged,
        "outputs_streamed": metrics["execution"]["outputs_streamed"],
        "auto_chunk": metrics.get("auto_chunk"),
        "native_admitted": (metrics.get("chunked_route") or {}).get("native_admitted"),
        "reroute_reason": (metrics.get("chunked_route") or {}).get("reroute_reason"),
        "staged_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "staged_bytes": path.stat().st_size,
    }
    Path(args.out).write_text(json.dumps(evidence, allow_nan=False, indent=1))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode_", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--workload", choices=WORKLOADS, required=True)
    prep.add_argument("--rows", type=int, required=True)
    prep.add_argument("--path", required=True)
    prep.set_defaults(func=prepare)
    runner = sub.add_parser("run")
    runner.add_argument("--workload", choices=WORKLOADS, required=True)
    runner.add_argument("--rows", type=int, required=True)
    runner.add_argument("--source", required=True)
    runner.add_argument("--mode", choices=("resident", "streamed"), required=True)
    runner.add_argument("--workdir", required=True)
    runner.add_argument("--out", required=True)
    runner.set_defaults(func=run)
    args = parser.parse_args(argv)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
