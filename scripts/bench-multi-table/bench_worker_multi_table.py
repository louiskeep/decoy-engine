"""Worker for the B7 multi-table merge benchmark (plan 2026-10-01-multi-table-dispatch,
section 11). One process, one configuration, one `run_pipeline` call.

Modes, all run by `bench_multi_table.py`:

`prepare --variant V --dir D`
    Build variant V's three fixed-seed sources and write each to D as Parquet,
    because `run_pipeline` profiles a source from its config's file path.

`baseline --variant V`
    Build the sources and report the process peak RSS without running anything,
    so a reader can see how much of every trial's RSS is source construction.

`run --variant V --split on|off --threads N --source-dir D --out-dir O`
    Build the same sources in this process, run `run_pipeline` with the default
    auto-chunk knobs (threshold 100,000 rows, chunk size 50,000), no vault writer and
    no validators, capture `ru_maxrss` as soon as it returns, then write each output
    as Arrow IPC and the JSON evidence for the parent. The parent does every comparison.

Workload: three independent tables built with B2's generator and column layout. For
table k, `rng = numpy.random.default_rng(seed_k)` with seeds 20261001, 20261002 and
20261003, `k = rng.integers(0, 10**9, size=rows, dtype=numpy.int64)`, and four
null-free `string` columns built element-wise from `k`: `email` (hash, namespace
`bench_ns_<k>`), `secret` (redact), `zip` (truncate, length 3), `ref` (passthrough).
`t1` and `t2` hold 1,000,000 rows and `t3` 50,000 (below the threshold, so it stays in
the full-frame group). Variant `extra` adds the unconfigured column `extra` to `t1` and
`t2`. Before B8 that rerouted both tables to B1's oracle route (`uncovered_columns`); with
B8 they run natively and the variant no longer measures a rerouted job.
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

DEFAULT_ROWS = 1_000_000
SMALL_ROWS = 50_000
SEEDS = {"t1": 20261001, "t2": 20261002, "t3": 20261003}
VARIANTS = ("base", "extra")
ENGINE_VERSION = "bench-multi-table"


def table_rows(name: str, big: int) -> int:
    return SMALL_ROWS if name == "t3" else big


def build_table(name: str, variant: str, rows: int) -> Any:
    import numpy
    import pyarrow as pa

    rng = numpy.random.default_rng(SEEDS[name])
    ks = rng.integers(0, 10**9, size=rows, dtype=numpy.int64).tolist()
    columns: dict[str, list[str]] = {
        "email": [f"user{v:09d}@example.com" for v in ks],
        "secret": [f"secret-{v:09d}" for v in ks],
        "zip": [f"{v:09d}" for v in ks],
        "ref": [f"keep-{v:09d}" for v in ks],
    }
    if variant == "extra" and name != "t3":
        columns["extra"] = [f"x{v:09d}" for v in ks]
    return pa.table({col: pa.array(values, pa.string()) for col, values in columns.items()})


def build_sources(variant: str, big: int) -> dict[str, Any]:
    return {name: build_table(name, variant, table_rows(name, big)) for name in SEEDS}


def build_config(source_dir: str) -> dict[str, Any]:
    from decoy_engine.config import PipelineConfig

    tables = []
    sources = {}
    targets = {}
    for index, name in enumerate(SEEDS, start=1):
        tables.append(
            {
                "name": name,
                "columns": [
                    {"name": "email", "strategy": "hash", "namespace": f"bench_ns_{index}"},
                    {"name": "secret", "strategy": "redact"},
                    {
                        "name": "zip",
                        "strategy": "truncate",
                        "provider_config": {"length": 3, "keep": "head"},
                    },
                    {"name": "ref", "strategy": "passthrough"},
                ],
            }
        )
        sources[name] = {
            "type": "file",
            "format": "parquet",
            "path": str(Path(source_dir) / f"{name}.parquet"),
        }
        targets[name] = {"type": "file", "format": "parquet", "path": "/dev/null"}
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 42, "post_validation": False},
            "sources": sources,
            "tables": tables,
            "targets": targets,
        }
    ).model_dump()


def _rss_bytes() -> int:
    # KiB on Linux, recorded in bytes.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def prepare(args: argparse.Namespace) -> int:
    import pyarrow.parquet as pq

    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, table in build_sources(args.variant, args.rows).items():
        pq.write_table(table, out / f"{name}.parquet")
    return 0


def baseline(args: argparse.Namespace) -> int:
    sources = build_sources(args.variant, args.rows)
    print(
        json.dumps({"rss_bytes": _rss_bytes(), "nbytes": {k: v.nbytes for k, v in sources.items()}})
    )
    return 0


def run(args: argparse.Namespace) -> int:
    import pyarrow as pa

    from decoy_engine.execution import run_pipeline

    sources = build_sources(args.variant, args.rows)
    config = build_config(args.source_dir)
    extra: dict[str, Any] = {"native_threads": args.threads}
    if args.split == "off":
        extra["multi_table_dispatch_enabled"] = False
    started = time.perf_counter()
    result = run_pipeline(config, sources=sources, engine_version=ENGINE_VERSION, **extra)
    wall_s = time.perf_counter() - started
    rss_bytes = _rss_bytes()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, list[str]] = {}
    for name, table in result.outputs.items():
        path = str(out_dir / f"{name}.arrow")
        with pa.OSFile(path, "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
            writer.write_table(table)
        metadata[name] = sorted(k.decode() for k in (table.schema.metadata or {}))
    metrics = result.quality_metrics
    evidence = {
        "wall_s": wall_s,
        "rss_bytes": rss_bytes,
        "auto_chunk": metrics.get("auto_chunk"),
        "chunked_route_by_table": metrics.get("chunked_route_by_table"),
        "source_nbytes": {k: v.nbytes for k, v in sources.items()},
        "output_schema_metadata": metadata,
        "output_names": list(result.outputs),
    }
    (out_dir / "evidence.json").write_text(json.dumps(evidence, allow_nan=False, indent=1))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--variant", choices=VARIANTS, required=True)
    prep.add_argument("--dir", required=True)
    prep.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    prep.set_defaults(func=prepare)
    base = sub.add_parser("baseline")
    base.add_argument("--variant", choices=VARIANTS, required=True)
    base.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    base.set_defaults(func=baseline)
    runner = sub.add_parser("run")
    runner.add_argument("--variant", choices=VARIANTS, required=True)
    runner.add_argument("--split", choices=("on", "off"), required=True)
    runner.add_argument("--threads", type=int, default=1)
    runner.add_argument("--source-dir", required=True)
    runner.add_argument("--out-dir", required=True)
    runner.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    runner.set_defaults(func=run)
    args = parser.parse_args(argv)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
