"""Task 4.5 D9 performance gate: ONE rep of the unified-slice production
lane, over the SAME frozen W2 workload `scripts/native-baseline/bench_
worker.py` masks, minus `pt_ts` (9 columns: 3 keyed-hash, 2 passthrough, 2
redact, 2 truncate) -- the mixed-strategy, hash-heavy shape D9 requires.

`pt_ts` is a timestamp column: the hardened admission domain (Codex
determination, CHANGE 1) admits only `{string, int64, bool}` for
passthrough, deliberately excluding every temporal type until it is a
separately-proven slice. Dropping it here (never touching the shared
`bench_worker.py` builder the native-route perf gate also reads) is what
keeps this worker's "real execution" vacuity guard below meaningful instead
of permanently unsatisfiable.

Run with `scripts/native-baseline/bench_driver.py --worker
../bench-unified-slice/bench_worker_unified.py` for the SAME external wall-
clock + VmHWM measurement the native-route perf gate uses. `bench_compare.py`
runs THIS worker for BOTH arms of its comparison, selected by the
`UNIFIED_BENCH_FLAG` environment variable ("off" = the legacy full-frame
route, "on" = the unified lane), so both arms mask one identical nine-column
workload and only the executed lane differs.

`auto_chunk=False` here is load-bearing (D9): a resident table at or above
`auto_chunk_threshold_rows` (default ~100k) is otherwise chunk-eligible, and
the unified slice declines a chunked disposition by construction -- forcing
`auto_chunk=False` is what makes the 100k/1M tiers a genuine full_frame,
non-chunked comparison instead of both arms silently taking the OLD chunked
route. `execution_mode="full_frame"` forces the same full_frame route on both
arms so `layer1_route`'s live re-derivation (inside the compiler) lands on
the identical disposition the off and on arms were measured under.

The D7 activation assertion at the end is the vacuity guard D9 requires:
a benchmark that silently fell back to the legacy oracle (a regression in
admission, an unavailable native companion) must FAIL LOUD here, not report
a misleadingly-fast "unified slice" number that never actually ran one.

Per-strategy timing (Codex final-gate HIGH): the 4.4 shadow coordinator
carries no per-node elapsed-time evidence (`OperatorCallEvidence`,
`execution/physical/_shadow_operators.py`, has no timing field), unlike the
legacy oracle's `TimingCollector` (`result.timings`, consumed by `bench_
worker.py`'s own per-strategy breakdown) or the native route's `kernel_
elapsed_s` (consumed by `bench_worker_native.py`). Splitting the combined
run's wall time by strategy would be a fabricated number, not a real one, so
`hash_ms` / `redact_ms` / `truncate_ms` / `passthrough_ms` are each measured
by a SEPARATE isolated unified-slice run over just that strategy's own
columns, at the same row count -- a real, directly-measured wall-clock
number per strategy, at the cost of running the pipeline five times per rep
instead of once. This is bench-script-only instrumentation; it does not
touch the lane or the shared 4.4 coordinator.

Sampled profiling reads Parquet, not CSV (Codex determination remediation):
`build_config` declares its source as CSV, but `tr_phone`/`tr_card` are
digit-only strings (`bench_worker.py`'s own `"512" + digits`), and a CSV
round-trip's default type sniffing reads an all-digit sampled column back as
int64 -- a genuine profile/resident TYPE MISMATCH the hardened admission
(CHANGE 1) now catches and correctly declines (previously invisible: no
prior check compared a non-hash node's resident type against anything). The
resident masking data was never affected (`sources` reads the Arrow table
directly, never the sample file); only the PROFILE the sample feeds was
wrong. Every worker call below writes its own Parquet sample and overrides
`cfg["sources"]["w2"]["format"]` to `"parquet"` post-`build_config` so the
profiler sees the SAME lossless types the resident source carries, matching
`_time_strategy`'s own sub-selection.

Usage: python bench_worker_unified.py <n_rows>
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.keyprovider import SecretKeyProvider

# Reuses the pinned W2 workload builder from the native-route baseline
# script (same fixed seed/schema/mask key) so both arms mask the identical
# frame; not duplicated here to avoid the two drifting apart. The path
# manipulation has to run before this import, so it cannot join the sorted
# block above.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native-baseline"))
from bench_worker import FIXED_MASK_KEY, build_config, build_sources

# Strategy -> its column subset in the frozen W2 schema (`bench_worker.
# build_config`); restated here (not derived from the config) so a schema
# change in `bench_worker.py` fails this module's own lookups loudly rather
# than silently timing an empty subset. `pt_ts` is excluded from passthrough
# (module docstring): outside the hardened admission domain.
_STRATEGY_COLUMNS: dict[str, tuple[str, ...]] = {
    "hash": ("h_email", "h_token", "h_uid"),
    "passthrough": ("pt_amount", "pt_flag"),
    "redact": ("rd_ssn", "rd_notes"),
    "truncate": ("tr_phone", "tr_card"),
}
# The one W2 column the hardened admission domain declines; dropped from
# both the resident source and the config before every unified-slice run
# below (main() and _time_strategy() alike) so the config's columns stay
# 1:1 with the resident schema (cheap_admission's own D3 requirement).
_UNADMITTED_COLUMN = "pt_ts"

# Which arm this worker times, selected by `bench_compare.py` via the environment
# so BOTH arms run this SAME nine-column source/config (a fair comparison): "on"
# = the unified-slice lane, "off" = the identical shape through the legacy route.
_FLAG_ON = os.environ.get("UNIFIED_BENCH_FLAG", "on").strip().lower() != "off"


def _write_sample_parquet(src: pa.Table, columns: tuple[str, ...], suffix: str) -> str:
    """A small representative Parquet sample of just `columns`, for
    profiling only (the masking pass reads the resident Arrow subset
    directly) -- mirrors `main()`'s own sampling for the full config.
    Parquet, not CSV (module docstring): preserves the resident Arrow type
    exactly, so the profile the sample feeds never disagrees with it."""
    fd, path = tempfile.mkstemp(prefix=f"w2_unified_{suffix}_", suffix=".parquet")
    os.close(fd)
    sample_n = min(src.num_rows, 2000)
    pq.write_table(src.select(list(columns)).slice(0, sample_n), path)
    return path


def _time_strategy(
    strategy: str,
    columns: tuple[str, ...],
    src: pa.Table,
    key_provider: SecretKeyProvider,
    *,
    flag_on: bool,
) -> float:
    """Real, isolated wall-clock milliseconds for masking ONLY `columns`
    (one strategy's own columns) at the same row count as the main combined
    run, on the SAME source for both arms. With `flag_on` it runs through the
    unified-slice lane (and raises loudly, matching the D7 vacuity guard, if
    that isolated run does not itself activate the slice); with `flag_on`
    False it runs the identical shape through the legacy route, so the two arms
    are compared over the same nine-column workload rather than different ones."""
    sample_path = _write_sample_parquet(src, columns, strategy)
    try:
        cfg = build_config(sample_path)
        cfg["sources"]["w2"]["format"] = "parquet"
        cfg["tables"][0]["columns"] = [
            col for col in cfg["tables"][0]["columns"] if col["name"] in columns
        ]
        sub_source = src.select(list(columns))
        t0 = time.perf_counter()
        result = run_pipeline(
            cfg,
            {"w2": sub_source},
            engine_version="unified-slice-4.5-bench",
            substrate="pandas",
            execution_mode="full_frame",
            auto_chunk=False,
            key_provider=key_provider,
            use_byte_estimate_routing=False,
            use_probe_routing=False,
            unified_slice_enabled=flag_on,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
    finally:
        try:
            os.unlink(sample_path)
        except OSError:
            pass

    if flag_on and result.quality_metrics.get("unified_slice_activation") is None:
        raise SystemExit(
            f"unified-slice isolated {strategy!r} timing run did not activate the "
            "unified slice -- refusing to record a fabricated per-strategy number."
        )
    return elapsed_ms


def main() -> None:
    n_rows = int(sys.argv[1])
    key_provider = SecretKeyProvider(secret=FIXED_MASK_KEY, key_version="v1")
    # Drop the one column outside the hardened admission domain (module
    # docstring) so the resident source and the config stay 1:1.
    src = build_sources(n_rows).drop_columns([_UNADMITTED_COLUMN])
    sources = {"w2": src}

    sample_n = min(n_rows, 2000)
    fd, source_path = tempfile.mkstemp(prefix="w2_unified_sample_", suffix=".parquet")
    os.close(fd)
    pq.write_table(src.slice(0, sample_n), source_path)
    cfg = build_config(source_path)
    cfg["sources"]["w2"]["format"] = "parquet"
    cfg["tables"][0]["columns"] = [
        col for col in cfg["tables"][0]["columns"] if col["name"] != _UNADMITTED_COLUMN
    ]

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
        unified_slice_enabled=_FLAG_ON,
    )
    t1 = time.perf_counter()

    activation = result.quality_metrics.get("unified_slice_activation")
    if _FLAG_ON:
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
    hash_cols = len(_STRATEGY_COLUMNS["hash"])

    per_strategy_ms: dict[str, float] = {
        strategy: _time_strategy(strategy, columns, src, key_provider, flag_on=_FLAG_ON)
        for strategy, columns in _STRATEGY_COLUMNS.items()
    }

    # Both arms run this same nine-column source/config; the fingerprint lets
    # bench_compare.py assert that equality per tier rather than trust it.
    admitted_columns = sorted(c for cols in _STRATEGY_COLUMNS.values() for c in cols)
    rec: dict[str, Any] = {
        "n_rows": n_rows,
        "wall_s": t1 - t0,
        "out_rows": out.num_rows,
        "execution_mode": "unified_slice" if _FLAG_ON else "legacy_full_frame",
        "hash_ms": per_strategy_ms["hash"],
        "hash_cols": hash_cols,
        "redact_ms": per_strategy_ms["redact"],
        "truncate_ms": per_strategy_ms["truncate"],
        "passthrough_ms": per_strategy_ms["passthrough"],
        "unified_slice_activated": _FLAG_ON,
        "plan_hash": activation["plan_hash"] if _FLAG_ON and activation is not None else None,
        "workload_fingerprint": {"n_rows": n_rows, "columns": admitted_columns},
    }
    print("BENCH_JSON " + json.dumps(rec))
    try:
        os.unlink(source_path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
