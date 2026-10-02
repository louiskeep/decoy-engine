"""Worker for the B2 auto-chunk merge benchmark (plan 2026-10-01-dispatcher-auto-chunk,
Design 11). One process, one configuration, one `run_pipeline` call.

Two modes, both run by `bench_auto_chunk.py`:

`prepare --variant V --path P`
    Build variant V's fixed-seed source and write it to P as Parquet, because
    `run_pipeline` profiles the source from its config's file path. Not measured.

`run --variant V --dispatcher on|off --threads N --source P --out-dir D`
    Build the same source in this process (so interpreter start, imports and
    source construction count toward the peak RSS, as they would in a production
    worker), run `run_pipeline` with the default auto-chunk knobs, capture
    `ru_maxrss` as soon as it returns, then write the output as Arrow IPC and the
    JSON evidence for the parent. The parent does every comparison.

Workload: B1's representative shape, 1M rows by default. `rng =
numpy.random.default_rng(20261001)`; `k = rng.integers(0, 10**9, size=rows,
dtype=numpy.int64)`; four null-free `string` columns built element-wise from `k`,
in this order: `email` (hash, namespace `bench_ns`), `secret` (redact), `zip`
(truncate, length 3), `ref` (passthrough). Variants: `base`; `extra` (base plus the
unconfigured column `extra`, so B1 reroutes to its oracle route); `pandas` (base
rebuilt through `pa.Table.from_pandas(df, preserve_index=False)` from a frame with
`df.attrs = {"bench": "b2"}`, so the source carries pandas schema metadata).
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any

SEED = 20261001
DEFAULT_ROWS = 1_000_000
VARIANTS = ("base", "extra", "pandas")
TABLE = "t"
ENGINE_VERSION = "bench-auto-chunk"


def build_table(variant: str, rows: int) -> Any:
    import numpy
    import pandas
    import pyarrow as pa

    rng = numpy.random.default_rng(SEED)
    ks = rng.integers(0, 10**9, size=rows, dtype=numpy.int64).tolist()
    columns: dict[str, list[str]] = {
        "email": [f"user{v:09d}@example.com" for v in ks],
        "secret": [f"secret-{v:09d}" for v in ks],
        "zip": [f"{v:09d}" for v in ks],
        "ref": [f"keep-{v:09d}" for v in ks],
    }
    if variant == "extra":
        columns["extra"] = [f"x{v:09d}" for v in ks]
    if variant == "pandas":
        frame = pandas.DataFrame(columns)
        frame.attrs = {"bench": "b2"}
        return pa.Table.from_pandas(frame, preserve_index=False)
    return pa.table({name: pa.array(values, pa.string()) for name, values in columns.items()})


def build_config(variant: str, source_path: str) -> dict[str, Any]:
    from decoy_engine.config import PipelineConfig

    columns = [
        {"name": "email", "strategy": "hash", "namespace": "bench_ns"},
        {"name": "secret", "strategy": "redact"},
        {
            "name": "zip",
            "strategy": "truncate",
            "provider_config": {"length": 3, "keep": "head"},
        },
        {"name": "ref", "strategy": "passthrough"},
    ]
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 42, "post_validation": False},
            "sources": {TABLE: {"type": "file", "format": "parquet", "path": source_path}},
            "tables": [{"name": TABLE, "columns": columns}],
            "targets": {TABLE: {"type": "file", "format": "parquet", "path": "/dev/null"}},
        }
    ).model_dump()


def prepare(args: argparse.Namespace) -> int:
    import pyarrow.parquet as pq

    pq.write_table(build_table(args.variant, args.rows), args.path)
    return 0


def run(args: argparse.Namespace) -> int:
    import pyarrow as pa

    from decoy_engine.execution import run_pipeline

    source = build_table(args.variant, args.rows)
    config = build_config(args.variant, args.source)
    extra: dict[str, Any] = {"native_threads": args.threads}
    if args.dispatcher == "off":
        extra["chunked_dispatcher_enabled"] = False
    started = time.perf_counter()
    result = run_pipeline(config, sources={TABLE: source}, engine_version=ENGINE_VERSION, **extra)
    wall_s = time.perf_counter() - started
    # KiB on Linux; recorded in bytes. Captured before any output is written.
    rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with pa.OSFile(str(out_dir / "output.arrow"), "wb") as sink:
        table = result.outputs[TABLE]
        with pa.ipc.new_file(sink, table.schema) as writer:
            writer.write_table(table)
    metrics = result.quality_metrics
    evidence = {
        "wall_s": wall_s,
        "rss_bytes": rss_bytes,
        "auto_chunk": metrics.get("auto_chunk"),
        "chunked_route": metrics.get("chunked_route"),
        "source_nbytes": source.nbytes,
        "output_schema_metadata": sorted(k.decode() for k in (table.schema.metadata or {})),
    }
    (out_dir / "evidence.json").write_text(json.dumps(evidence, allow_nan=False, indent=1))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--variant", choices=VARIANTS, required=True)
    prep.add_argument("--path", required=True)
    prep.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    prep.set_defaults(func=prepare)
    runner = sub.add_parser("run")
    runner.add_argument("--variant", choices=VARIANTS, required=True)
    runner.add_argument("--dispatcher", choices=("on", "off"), required=True)
    runner.add_argument("--threads", type=int, default=1)
    runner.add_argument("--source", required=True)
    runner.add_argument("--out-dir", required=True)
    runner.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    runner.set_defaults(func=run)
    args = parser.parse_args(argv)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
