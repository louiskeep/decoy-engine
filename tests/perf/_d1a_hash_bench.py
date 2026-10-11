"""Predeclared D1a micro-benchmark: native vs Python out-of-core hash on a frozen workload.

One mode per process so each run's peak RSS is its own (the gate is on TOTAL process peak
RSS, with the post-import baseline reported separately). Workload, frozen before any
measurement (docs/plans/2026-10-11-phase-d-d1a-hash-native-ooc.md section 6):

* 5,000,000 rows, one utf8 column, ASCII alphanumerics, length uniform on [8, 40] (mean 24),
  a fixed 5% null fraction, no post-Unicode-version codepoints; a seeded numpy generator keyed
  on (SEED, batch index), so every round and both modes hash identical bytes.
* batch size 65,536; native threads = 4; the timed region wraps `mask_batch` only (generation
  and the output digest are outside it) and every output batch is consumed into a digest.
* native: 1 warmup round discarded + 7 measured; python: 3 measured (p95 over 3 samples is
  coarse and reported as such).

Run as `python -m tests.perf._d1a_hash_bench --mode native|python`; prints one JSON object.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

SEED = 20261011
ROWS = 5_000_000
BATCH_ROWS = 65_536
THREADS = 4
MIN_LEN, MAX_LEN = 8, 40
NULL_FRACTION = 0.05
ALPHABET = np.frombuffer(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", dtype=np.uint8
)
ROUNDS = {"native": (1, 7), "python": (0, 3)}  # (warmup, measured)


def batch_at(index: int) -> pa.RecordBatch:
    """The index-th batch of the frozen workload (deterministic in `index` alone)."""
    n = min(BATCH_ROWS, ROWS - index * BATCH_ROWS)
    rng = np.random.default_rng([SEED, index])
    is_null = rng.random(n) < NULL_FRACTION
    lengths = rng.integers(MIN_LEN, MAX_LEN + 1, n).astype(np.int32)
    lengths[is_null] = 0
    offsets = np.zeros(n + 1, dtype=np.int32)
    np.cumsum(lengths, out=offsets[1:])
    data = ALPHABET[rng.integers(0, len(ALPHABET), int(offsets[-1]))]
    validity = np.packbits(~is_null, bitorder="little")
    column = pa.Array.from_buffers(
        pa.string(),
        n,
        [pa.py_buffer(validity), pa.py_buffer(offsets), pa.py_buffer(data)],
        null_count=int(is_null.sum()),
    )
    return pa.record_batch([column], names=["c"])


def _plan() -> Any:
    from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed

    seed = ColumnSeed(
        namespace="ns_bench",
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )
    table = TableSeed(per_column=(("c", seed),), per_group=())
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(job_seed=b"\x11" * 8, per_table=(("t", table),))
    )


def _peak_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KiB on Linux


def _consume(digest: Any, col: pa.Array) -> None:
    """Fold one output column into the digest by logical content, not buffer padding."""
    valid = np.asarray(pc.is_valid(col).to_numpy(zero_copy_only=False), dtype=np.uint8)
    digest.update(valid.tobytes())
    filled = pc.fill_null(col, "")
    _, offsets_buf, data_buf = filled.buffers()
    offsets = np.frombuffer(offsets_buf, dtype=np.int32)[
        filled.offset : filled.offset + len(filled) + 1
    ]
    digest.update(np.diff(offsets).tobytes())
    digest.update(memoryview(data_buf)[int(offsets[0]) : int(offsets[-1])])


def _percentile(samples: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(samples), q))


def run(mode: str) -> dict[str, Any]:
    from decoy_engine.execution.out_of_core._hash_run import OocHashRun
    from decoy_engine.execution.out_of_core._mask import mask_batch

    plan = _plan()
    hash_plan = None
    if mode == "native":
        table_plan = OocHashRun(native_threads=THREADS).prepare_table(
            plan, "t", pa.schema([pa.field("c", pa.string())]), frozenset(), (), site="bench"
        )
        decision = table_plan.decisions["c"]
        if not decision.native:
            raise SystemExit(
                f"native mode requested but the column was declined: {decision.reason}"
            )
        hash_plan = table_plan
    baseline_mib = _peak_rss_mib()
    warmup, measured = ROUNDS[mode]
    batches = -(-ROWS // BATCH_ROWS)
    times: list[float] = []
    digests: list[str] = []
    for round_no in range(warmup + measured):
        elapsed = 0.0
        digest = hashlib.sha256()
        for i in range(batches):
            batch = batch_at(i)
            start = time.perf_counter()
            out = mask_batch(plan, "t", batch, hash_plan=hash_plan)
            elapsed += time.perf_counter() - start
            _consume(digest, out.column(0))
        digests.append(digest.hexdigest())
        if round_no >= warmup:
            times.append(elapsed)
    peak_mib = _peak_rss_mib()
    return {
        "mode": mode,
        "rows": ROWS,
        "batch_rows": BATCH_ROWS,
        "native_threads": THREADS if mode == "native" else None,
        "rounds_measured": len(times),
        "seconds": times,
        "p50_seconds": _percentile(times, 50),
        "p95_seconds": _percentile(times, 95),
        "baseline_rss_mib": baseline_mib,
        "peak_rss_mib": peak_mib,
        "delta_rss_mib": peak_mib - baseline_mib,
        "output_digest": digests[0],
        "digest_stable_across_rounds": len(set(digests)) == 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=sorted(ROUNDS), required=True)
    print(json.dumps(run(parser.parse_args().mode)))


if __name__ == "__main__":
    main()
