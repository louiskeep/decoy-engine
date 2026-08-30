"""W2 NATIVE-route worker: ONE rep of Task 2.7's native chunked dispatch.

Same frozen W2 workload as `bench_worker.py` (10 columns, fixed seed, fixed
32-byte mask key), but routes through `run_native_or_oracle_chunked` -- the
Task 2.7 dispatch inside the Phase 1 streaming coordinator -- instead of the
pandas full-frame oracle.

The source is a PRE-BUILT Parquet file (`build_w2_parquet.py`, run once per
tier by the driver, OUTSIDE this process): this worker only opens it and reads
it LAZILY in batches (`ParquetFile.iter_batches`), so the process the external
VmHWM sampler measures never holds more than one input batch and one output
batch at a time. Building the source data inside this same process (as an
earlier version of this script did) would inflate peak RSS with the whole
table's residency, which measures input-holding, not the streaming route --
exactly the full-frame cost target 5 exists to contrast against. Each masked
batch is dropped immediately after counting its rows (never accumulated).

Invoked once per rep in a FRESH OS process (the driver spawns it under
/usr/bin/time -v so peak RSS is measured externally, whole-process).

Usage: python bench_worker_native.py <n_rows> <parquet_path> [batch_rows]
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.execution.native import NativeRouteEvidence, run_native_or_oracle_chunked
from decoy_engine.keyprovider import SecretKeyProvider

# ---- Frozen W2 constants (identical to bench_worker.py) -------------------
FIXED_SEED = 20260828
FIXED_MASK_KEY = bytes.fromhex("a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90")

N_HASH_COLS = 3
DEFAULT_BATCH_ROWS = 50_000  # streaming batch size; independent of n_rows


def _stream_parquet(path: str, batch_rows: int) -> Iterator[pa.Table]:
    for record_batch in pq.ParquetFile(path).iter_batches(batch_size=batch_rows):
        yield pa.Table.from_batches([record_batch])


def _sample_table(path: str, sample_n: int) -> pa.Table:
    """A bounded sample for profiling, read from ONE row group only.

    Never reads the whole file: row group 0 already caps at the writer's own
    batch size (<=50,000 rows), so this stays O(sample_n) regardless of the
    file's total row count.
    """
    pf = pq.ParquetFile(path)
    if pf.num_row_groups == 0:
        return pf.schema_arrow.empty_table()
    return pf.read_row_group(0).slice(0, sample_n)


def build_config(source_path: str) -> dict:
    raw = {
        "version": 1,
        "global_settings": {"seed": FIXED_SEED, "post_validation": False},
        "sources": {"w2": {"type": "file", "format": "csv", "path": source_path}},
        "targets": {"w2": {"type": "file", "format": "csv", "path": "/dev/null"}},
        "tables": [
            {
                "name": "w2",
                "columns": [
                    {"name": "h_email", "strategy": "hash", "namespace": "ns_email"},
                    {"name": "h_token", "strategy": "hash", "namespace": "ns_token"},
                    {"name": "h_uid", "strategy": "hash", "namespace": "ns_uid"},
                    {"name": "pt_amount", "strategy": "passthrough"},
                    {"name": "pt_flag", "strategy": "passthrough"},
                    {"name": "pt_ts", "strategy": "passthrough"},
                    {"name": "rd_ssn", "strategy": "redact"},
                    {"name": "rd_notes", "strategy": "redact"},
                    {
                        "name": "tr_phone",
                        "strategy": "truncate",
                        "provider_config": {"length": 3, "keep": "head"},
                    },
                    {
                        "name": "tr_card",
                        "strategy": "truncate",
                        "provider_config": {"length": 4, "keep": "tail"},
                    },
                ],
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def main() -> None:
    import csv
    import os
    import tempfile

    n_rows = int(sys.argv[1])
    parquet_path = sys.argv[2]
    batch_rows = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_BATCH_ROWS
    key_provider = SecretKeyProvider(secret=FIXED_MASK_KEY, key_version="v1")

    # A small representative CSV satisfies the "mask tables require a declared
    # source" schema rule and gives the dispatch's own first-chunk profiling a
    # real file to point at; bounded to one row group regardless of n_rows.
    sample_n = min(n_rows, 2000)
    sample = _sample_table(parquet_path, sample_n)
    fd, source_path = tempfile.mkstemp(prefix="w2_native_sample_", suffix=".csv")
    os.close(fd)
    sample.to_pandas().to_csv(source_path, index=False, quoting=csv.QUOTE_MINIMAL)
    del sample
    cfg = build_config(source_path)

    sink: list[NativeRouteEvidence] = []
    out_rows = 0
    t0 = time.perf_counter()
    for masked in run_native_or_oracle_chunked(
        cfg,
        _stream_parquet(parquet_path, batch_rows),
        table="w2",
        engine_version="phase2-gate-bench",
        key_provider=key_provider,
        route_evidence_sink=sink,
    ):
        out_rows += masked.num_rows  # drop the batch immediately; never accumulate
    t1 = time.perf_counter()

    evidence = sink[0]
    hash_ms = evidence.kernel_elapsed_s.get("hash", 0.0) * 1000.0
    rec = {
        "n_rows": n_rows,
        "batch_rows": batch_rows,
        "wall_s": t1 - t0,
        "out_rows": out_rows,
        "native_admitted": evidence.native_admitted,
        "reroute_reason": evidence.reroute_reason,
        "compiled_kernel_executed": evidence.compiled_kernel_executed,
        "hash_ms": hash_ms,
        "hash_cols": N_HASH_COLS if evidence.native_admitted else 0,
        "redact_ms": evidence.kernel_elapsed_s.get("redact", 0.0) * 1000.0,
        "truncate_ms": evidence.kernel_elapsed_s.get("truncate", 0.0) * 1000.0,
        "passthrough_ms": evidence.kernel_elapsed_s.get("passthrough", 0.0) * 1000.0,
        "execution_mode": "native_streaming" if evidence.native_admitted else "oracle_fallback",
    }
    print("BENCH_JSON " + json.dumps(rec))
    try:
        os.unlink(source_path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
