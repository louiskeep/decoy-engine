"""Predeclared D1b-i micro-benchmark: native vs Python out-of-core bucket_perturb, frozen workload.

One mode per process so each run's peak RSS is its own (the gate is on TOTAL process peak RSS,
with the post-import baseline reported separately). Workload, frozen before any measurement
(docs/plans/2026-10-11-phase-d-d1b-i-bucket-perturb-native-ooc.md section 6):

* 5,000,000 rows of one utf8 date column in the fixed format `%Y-%m-%d`, dates uniform over
  1990-01-01..2029-12-31 (all inside the native operator's pandas Timestamp range), a fixed 5%
  null fraction and a fixed 2% unparseable fraction, bucket = month; a seeded generator keyed on
  (SEED, batch index), so every round and both modes see identical bytes.
* batch size 65,536; native threads = 4; the timed region wraps `mask_batch` only (generation
  and the output digest are outside it) and every output batch is consumed into a digest.
* native: 1 warmup round discarded + 7 measured; python: 3 measured (p95 over 3 samples is
  coarse and reported as such).

Run as `python -m tests.perf._d1b1_bucket_bench --mode native|python`; prints one JSON object.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pyarrow as pa

from tests.perf._d1a_hash_bench import (
    BATCH_ROWS,
    ROUNDS,
    ROWS,
    THREADS,
    _consume,
    _peak_rss_mib,
    _percentile,
)

SEED = 20261012
NULL_FRACTION = 0.05
UNPARSEABLE_FRACTION = 0.02
_FIRST_DAY = np.datetime64("1990-01-01")
_DAYS = 14_610  # through 2029-12-31
_UNPARSEABLE = "not-a-date"


def batch_at(index: int) -> pa.RecordBatch:
    """The index-th batch of the frozen workload (deterministic in `index` alone)."""
    n = min(BATCH_ROWS, ROWS - index * BATCH_ROWS)
    rng = np.random.default_rng([SEED, index])
    days = _FIRST_DAY + rng.integers(0, _DAYS, n).astype("timedelta64[D]")
    text = np.datetime_as_string(days, unit="D").astype(object)
    draw = rng.random(n)
    is_null = draw < NULL_FRACTION
    text[(draw >= NULL_FRACTION) & (draw < NULL_FRACTION + UNPARSEABLE_FRACTION)] = _UNPARSEABLE
    column = pa.array(text, pa.string(), mask=is_null)
    return pa.record_batch([column], names=["c"])


def _plan() -> Any:
    from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed

    seed = ColumnSeed(
        namespace="ns_bench",
        strategy="bucket_perturb",
        provider="bucket_perturb",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(("bucket", "month"), ("date_format", "%Y-%m-%d")),
        coherent_with=(),
    )
    table = TableSeed(per_column=(("c", seed),), per_group=())
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(job_seed=b"\x11" * 8, per_table=(("t", table),))
    )


def run(mode: str) -> dict[str, Any]:
    from decoy_engine.execution.out_of_core._hash_run import OocHashRun
    from decoy_engine.execution.out_of_core._mask import mask_batch

    plan = _plan()
    hash_plan = None
    if mode == "native":
        table_plan = OocHashRun(native_threads=THREADS).prepare_table(
            plan, "t", pa.schema([pa.field("c", pa.string())]), frozenset(), (), site="bench"
        )
        assert table_plan.bucket is not None
        decision = table_plan.bucket.decisions["c"]
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
        "bucket": "month",
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
