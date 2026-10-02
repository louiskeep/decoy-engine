"""Worker for the B8 merge benchmark (plan 2026-10-01-native-unconfigured-passthrough,
Design 11). One process, one configuration, one `run_mask_chunked` call.

`run --unconfigured N --policy warn|error --out-dir D [--measure]`
    Build the source in this process (interpreter start, imports and source construction
    count toward the peak RSS), run `run_mask_chunked` over zero-copy 50,000-row slices,
    drain the iterator, and capture `ru_maxrss` immediately after the drain, before any
    artifact is written. Then write the concatenated output as Arrow IPC and the
    per-chunk warnings and route evidence as JSON. The parent does every comparison.

Workload: `rng = numpy.random.default_rng(20261001)`; `k` is 1,000,000 int64 built
element-wise (no pandas). Four configured string columns: `email` (hash, namespace
`bench_ns`), `secret` (redact), `zip` (truncate, length 3), `ref` (passthrough). Then N
unconfigured columns, in this order: `u_int` int64 = k; `u_float` float64 = k / 7;
`u_str` string = f"u{v:09d}"; `u_date` date32 = k % 2932896 days; `u_ts` timestamp[us, UTC]
= k microseconds; `u_bool` bool = k % 2 == 0; `u_dec` decimal128(18,2) = k / 100;
`u_dict` dictionary<int32, string> over 64 values f"cat{v % 64}". Every fifth row of
`u_int`, `u_str` and `u_date` is null.
"""

from __future__ import annotations

import argparse
import decimal
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any

SEED = 20261001
DEFAULT_ROWS = 1_000_000
CHUNK_ROWS = 50_000
TABLE = "t"
ENGINE_VERSION = "bench-unconfigured-passthrough"
UNCONFIGURED = ("u_int", "u_float", "u_str", "u_date", "u_ts", "u_bool", "u_dec", "u_dict")


def build_table(rows: int, unconfigured: int) -> Any:
    import numpy
    import pyarrow as pa

    rng = numpy.random.default_rng(SEED)
    ks = rng.integers(0, 10**9, size=rows, dtype=numpy.int64).tolist()
    nulls = [i % 5 == 4 for i in range(rows)]
    columns: dict[str, Any] = {
        "email": pa.array([f"user{v:09d}@example.com" for v in ks], pa.string()),
        "secret": pa.array([f"secret-{v:09d}" for v in ks], pa.string()),
        "zip": pa.array([f"{v:09d}" for v in ks], pa.string()),
        "ref": pa.array([f"keep-{v:09d}" for v in ks], pa.string()),
    }
    builders = {
        "u_int": lambda: pa.array(
            [None if n else v for v, n in zip(ks, nulls, strict=True)], pa.int64()
        ),
        "u_float": lambda: pa.array([v / 7 for v in ks], pa.float64()),
        "u_str": lambda: pa.array(
            [None if n else f"u{v:09d}" for v, n in zip(ks, nulls, strict=True)], pa.string()
        ),
        "u_date": lambda: pa.array(
            [None if n else v % 2932896 for v, n in zip(ks, nulls, strict=True)], pa.date32()
        ),
        "u_ts": lambda: pa.array(ks, pa.timestamp("us", tz="UTC")),
        "u_bool": lambda: pa.array([v % 2 == 0 for v in ks], pa.bool_()),
        "u_dec": lambda: pa.array([decimal.Decimal(v) / 100 for v in ks], pa.decimal128(18, 2)),
        "u_dict": lambda: pa.array([f"cat{v % 64}" for v in ks], pa.string()).dictionary_encode(),
    }
    for name in UNCONFIGURED[:unconfigured]:
        columns[name] = builders[name]()
    return pa.table(columns)


def build_config(policy: str) -> dict[str, Any]:
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
            "global_settings": {
                "seed": 42,
                "post_validation": False,
                "unconfigured_column_policy": policy,
            },
            "sources": {TABLE: {"type": "file", "format": "parquet", "path": "/dev/null"}},
            "tables": [{"name": TABLE, "columns": columns}],
            "targets": {TABLE: {"type": "file", "format": "parquet", "path": "/dev/null"}},
        }
    ).model_dump()


def run(args: argparse.Namespace) -> int:
    import pyarrow as pa

    from decoy_engine import run_mask_chunked
    from decoy_engine.keyprovider import SecretKeyProvider

    source = build_table(args.rows, args.unconfigured)
    chunks = [source.slice(i, CHUNK_ROWS) for i in range(0, source.num_rows, CHUNK_ROWS)]
    config = build_config(args.policy)
    sink: list[Any] = []
    ev: list[Any] = []
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    gen = run_mask_chunked(
        config,
        chunks,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=SecretKeyProvider(secret=bytes(range(32)), key_version="v1"),
        native_threads=args.threads,
        chunk_result_sink=sink,
        route_evidence_sink=ev,
    )
    outputs: list[Any] = []
    error: dict[str, Any] | None = None
    try:
        for out in gen:
            outputs.append(out)
    except Exception as exc:
        error = {"type": type(exc).__name__, "code": getattr(exc, "code", None)}
    wall_s = time.perf_counter() - started
    # KiB on Linux; recorded in bytes. Captured before any artifact is written.
    rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    if outputs:
        table = pa.concat_tables(outputs)
        with (
            pa.OSFile(str(out_dir / "output.arrow"), "wb") as handle,
            pa.ipc.new_file(handle, table.schema) as writer,
        ):
            writer.write_table(table)
    evidence = {
        "wall_s": wall_s,
        "rss_bytes": rss_bytes,
        "source_nbytes": source.nbytes,
        "chunks": len(outputs),
        "error": error,
        "native_admitted": ev[0].native_admitted,
        "reroute_reason": ev[0].reroute_reason,
        "warnings": [
            [{"code": w.code, "detail": w.detail} for w in result.warnings] for result in sink
        ],
        "chunked_route": sink[0].quality_metrics["chunked_route"] if sink else None,
    }
    (out_dir / "evidence.json").write_text(json.dumps(evidence, allow_nan=False, indent=1))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--unconfigured", type=int, choices=range(0, 9), required=True)
    runner.add_argument("--policy", choices=("warn", "error"), default="warn")
    runner.add_argument("--threads", type=int, default=1)
    runner.add_argument("--out-dir", required=True)
    runner.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    runner.set_defaults(func=run)
    args = parser.parse_args(argv)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
