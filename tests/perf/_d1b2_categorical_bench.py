"""Predeclared D1b-ii micro-benchmark: native vs Python out-of-core categorical, frozen workload.

One mode per process so each run's peak RSS is its own (the gate is on TOTAL process peak RSS,
with the post-import baseline reported separately). Workload, frozen before any measurement
(docs/plans/2026-10-11-phase-d-d1b-ii-categorical-native-ooc.md section 6):

* 5,000,000 rows of one utf8 source column, mapped onto a fixed 12-category weighted pool; the
  source is the D1a frozen text workload (ASCII alphanumerics, length uniform on [8, 40], a
  fixed 5% null fraction, a seeded generator keyed on (SEED, batch index)), so every round and
  both modes see identical bytes.
* batch size 65,536; native threads = 4; the timed region wraps `mask_batch` only (generation
  and the output digest are outside it) and every output batch is consumed into a digest.
* native: 1 warmup round discarded + 7 measured; python: 3 measured (p95 over 3 samples is
  coarse and reported as such).

Run as `python -m tests.perf._d1b2_categorical_bench --mode native|python`; prints one JSON object.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from tests.perf._d1a_hash_bench import (
    BATCH_ROWS,
    ROUNDS,
    ROWS,
    THREADS,
    _consume,
    _peak_rss_mib,
    _percentile,
    batch_at,
)

POOL = (
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
)
WEIGHTS = (30, 20, 12, 9, 8, 6, 5, 4, 3, 1.5, 1, 0.5)


def _plan() -> Any:
    from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed

    seed = ColumnSeed(
        namespace="ns_bench",
        strategy="categorical",
        provider="categorical",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(("categories", POOL), ("weights", WEIGHTS)),
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
        assert table_plan.categorical is not None
        decision = table_plan.categorical.decisions["c"]
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
        "pool_size": len(POOL),
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
