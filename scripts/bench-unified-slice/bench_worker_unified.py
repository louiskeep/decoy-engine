"""Task 4.5 D9 performance gate: ONE rep of the unified-slice production
lane, over the SAME frozen W2 workload `scripts/native-baseline/bench_
worker.py` masks (10 columns: 3 keyed-hash, 3 passthrough, 2 redact, 2
truncate) -- the mixed-strategy, hash-heavy shape D9 requires.

Run with `scripts/native-baseline/bench_driver.py --worker
../bench-unified-slice/bench_worker_unified.py` for the SAME external wall-
clock + VmHWM measurement the native-route perf gate uses, so the two arms
(this worker vs. the unmodified `bench_worker.py` oracle) are measured
identically and only the executed lane differs.

`auto_chunk=False` here is load-bearing (D9): a resident table at or above
`auto_chunk_threshold_rows` (default ~100k) is otherwise chunk-eligible, and
the unified slice declines a chunked disposition by construction -- forcing
`auto_chunk=False` is what makes the 100k/1M tiers a genuine full_frame,
non-chunked comparison instead of both arms silently taking the OLD chunked
route. `execution_mode="full_frame"` mirrors the oracle worker's own forced
route so `layer1_route`'s live re-derivation (inside the compiler) lands on
the identical disposition both workers were measured under.

The D7 activation assertion at the end is the vacuity guard D9 requires:
a benchmark that silently fell back to the legacy oracle (a regression in
admission, an unavailable native companion) must FAIL LOUD here, not report
a misleadingly-fast "unified slice" number that never actually ran one.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.keyprovider import SecretKeyProvider

# Reuses the pinned W2 workload builder from the native-route baseline
# script (same fixed seed/schema/mask key) so both arms mask the identical
# frame; not duplicated here to avoid the two drifting apart. The path
# manipulation has to run before this import, so it cannot join the sorted
# block above.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native-baseline"))
from bench_worker import FIXED_MASK_KEY, build_config, build_sources


def main() -> None:
    import csv
    import os
    import tempfile

    n_rows = int(sys.argv[1])
    key_provider = SecretKeyProvider(secret=FIXED_MASK_KEY, key_version="v1")
    src = build_sources(n_rows)
    sources = {"w2": src}

    sample_n = min(n_rows, 2000)
    fd, source_path = tempfile.mkstemp(prefix="w2_unified_sample_", suffix=".csv")
    os.close(fd)
    src.slice(0, sample_n).to_pandas().to_csv(source_path, index=False, quoting=csv.QUOTE_MINIMAL)
    cfg = build_config(source_path)

    t0 = time.perf_counter()
    result = run_pipeline(
        cfg,
        sources,
        engine_version="unified-slice-4.5-bench",
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        key_provider=key_provider,
        use_byte_estimate_routing=False,
        use_probe_routing=False,
        unified_slice_enabled=True,
    )
    t1 = time.perf_counter()

    activation = result.quality_metrics.get("unified_slice_activation")
    if activation is None:
        raise SystemExit(
            "unified-slice benchmark worker did not actually execute the unified "
            "slice (quality_metrics carries no 'unified_slice_activation' leaf) -- "
            "refusing to record a silent legacy-oracle fallback as a unified-slice "
            "result. Check admission (native companion availability, config shape)."
        )
    for node_id, evidence in activation["nodes"].items():
        if not evidence["executed"]:
            raise SystemExit(f"node {node_id!r} reports executed=False in its own evidence")

    out = result.outputs["w2"]
    hash_ms = 0.0
    hash_cols = 0
    redact_ms = 0.0
    truncate_ms = 0.0
    passthrough_ms = 0.0
    for node_id in activation["nodes"]:
        # `activation["nodes"]` keys are `f"{table}:{columns}:{kind}:{strategy}"`
        # (see PhysicalNode.node_id); the strategy is the last colon-segment.
        strategy = node_id.rsplit(":", 1)[-1]
        if strategy == "hash":
            hash_cols += 1

    rec = {
        "n_rows": n_rows,
        "wall_s": t1 - t0,
        "out_rows": out.num_rows,
        "execution_mode": "unified_slice",
        "hash_ms": hash_ms,
        "hash_cols": hash_cols,
        "redact_ms": redact_ms,
        "truncate_ms": truncate_ms,
        "passthrough_ms": passthrough_ms,
        "unified_slice_activated": True,
        "plan_hash": activation["plan_hash"],
    }
    print("BENCH_JSON " + json.dumps(rec))
    try:
        os.unlink(source_path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
