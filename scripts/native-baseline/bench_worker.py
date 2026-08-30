"""W2 baseline worker: ONE rep of the pinned pandas full-frame oracle.

Runs exactly one masking pass over the frozen W2 workload at the row count
given on argv, and prints a one-line JSON record to stdout with the timed
wall (perf_counter around run_pipeline only) and the row count.

Invoked once per rep in a FRESH OS process (the driver spawns it under
/usr/bin/time -v so peak RSS is measured externally, whole-process).

Usage: python bench_worker.py <n_rows>
"""

from __future__ import annotations

import json
import sys
import time

import numpy as np
import pyarrow as pa

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.keyprovider import SecretKeyProvider

# ---- Frozen W2 constants --------------------------------------------------
FIXED_SEED = 20260828
# Fixed 32-byte mask key (keyed hash), hard-coded for reproducibility.
FIXED_MASK_KEY = bytes.fromhex("a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90")

N_HASH_COLS = 3  # keyed-hash columns = dominant cost (task: at least 3)


def build_sources(n_rows: int) -> pa.Table:
    """Deterministic, fixed-order W2 source frame with 10 typed columns.

    Built with vectorized numpy so source construction stays cheap relative to
    the masking pass. Column input types span utf8, int64, bool, and
    timestamp-with-timezone (spread across the 10 columns).
    """
    ids = np.arange(n_rows, dtype=np.int64)
    ids_str = ids.astype("U")  # vectorized int->str in C

    # 3 keyed-hash inputs: two utf8, one int64.
    h_email = pa.array(np.char.add(np.char.add("user", ids_str), "@example.com"))
    h_token = pa.array(np.char.add("tok_", ids_str))
    h_uid = pa.array((100_000_000 + ids), type=pa.int64())

    # passthrough inputs: int64, bool, timestamp-with-timezone.
    pt_amount = pa.array((ids * 13) % 1_000_000, type=pa.int64())
    pt_flag = pa.array((ids % 2 == 0), type=pa.bool_())
    pt_ts = pa.array(
        (1_600_000_000_000_000 + ids * 1_000_000).astype(np.int64),
        type=pa.timestamp("us", tz="UTC"),
    )

    # redact inputs: utf8.
    rd_ssn = pa.array(np.char.add(np.char.add("5", np.mod(ids, 900).astype("U")), "-11-2222"))
    rd_notes = pa.array(np.char.add("note-", ids_str))

    # truncate inputs: utf8.
    tr_phone = pa.array(np.char.add("512", np.mod(ids, 9000).astype("U")))
    tr_card = pa.array(np.char.add("4000", np.mod(ids, 9999).astype("U")))

    return pa.table(
        {
            "h_email": h_email,
            "h_token": h_token,
            "h_uid": h_uid,
            "pt_amount": pt_amount,
            "pt_flag": pt_flag,
            "pt_ts": pt_ts,
            "rd_ssn": rd_ssn,
            "rd_notes": rd_notes,
            "tr_phone": tr_phone,
            "tr_card": tr_card,
        }
    )


def build_config(source_path: str) -> dict:
    raw = {
        "version": 1,
        "global_settings": {"seed": FIXED_SEED, "post_validation": False},
        "sources": {
            "w2": {"type": "file", "format": "csv", "path": source_path},
        },
        "targets": {
            "w2": {"type": "file", "format": "csv", "path": "/dev/null"},
        },
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
    key_provider = SecretKeyProvider(secret=FIXED_MASK_KEY, key_version="v1")
    src = build_sources(n_rows)
    sources = {"w2": src}

    # Profiling reads a SAMPLE from the declared source file (nrows-limited),
    # independent of the full caller Arrow source the masking adapter consumes.
    # A small representative CSV keeps profile I/O bounded and off the masking
    # hot path while satisfying the "mask tables require a declared source"
    # schema rule.
    sample_n = min(n_rows, 2000)
    fd, source_path = tempfile.mkstemp(prefix="w2_sample_", suffix=".csv")
    os.close(fd)
    src.slice(0, sample_n).to_pandas().to_csv(source_path, index=False, quoting=csv.QUOTE_MINIMAL)
    cfg = build_config(source_path)

    t0 = time.perf_counter()
    result = run_pipeline(
        cfg,
        sources,
        engine_version="phase2-baseline",
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        key_provider=key_provider,
        # full_frame is forced, so route-selection signals are moot. Disable
        # them to bypass an engine size-estimator crash on tz-timestamp cols
        # (sample_average_string_bytes calls .encode() on a datetime). Has no
        # effect on the executed masking path.
        use_byte_estimate_routing=False,
        use_probe_routing=False,
    )
    t1 = time.perf_counter()

    out = result.outputs["w2"]
    exec_mode = None
    try:
        exec_mode = result.quality_metrics.get("execution", {}).get("execution_mode")
    except Exception:
        pass

    # Per-strategy in-process timing breakdown (excludes source build + profile).
    hash_ms = 0.0
    hash_cols = 0
    redact_ms = 0.0
    truncate_ms = 0.0
    passthrough_ms = 0.0
    for rec_t in result.timings:
        st = rec_t.strategy_type
        if st == "hash":
            hash_ms += rec_t.elapsed_ms
            hash_cols += 1
        elif st == "redact":
            redact_ms += rec_t.elapsed_ms
        elif st == "truncate":
            truncate_ms += rec_t.elapsed_ms
        elif st == "passthrough":
            passthrough_ms += rec_t.elapsed_ms

    rec = {
        "n_rows": n_rows,
        "wall_s": t1 - t0,
        "out_rows": out.num_rows,
        "execution_mode": exec_mode,
        "hash_ms": hash_ms,
        "hash_cols": hash_cols,
        "redact_ms": redact_ms,
        "truncate_ms": truncate_ms,
        "passthrough_ms": passthrough_ms,
    }
    print("BENCH_JSON " + json.dumps(rec))
    try:
        os.unlink(source_path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
