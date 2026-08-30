"""Task 3.6: native+streaming C1 bench (peak RSS + wall) vs the pinned oracle.

Mirrors `bench_c1_oracle.py`'s out-of-band build + fresh-process worker +
external `VmHWM` staging, run against `run_native_or_oracle_chunked` (the
compiled companion present) instead of the pinned pandas oracle. Reuses
`bench_c1_oracle.py`'s data generation, the frozen recipe (`build_config`),
and its fresh-process/VmHWM harness directly (imported, not re-typed), so the
native and oracle numbers this script reports come from the exact same
recipe and tiers `docs/plans/PHASE3-C1-BASELINE.md` froze -- never a
drifted approximation.

The native route STREAMS in chunks (`chunk_size_rows`, the frozen JC-3 value
of 50,000), so its peak RSS is expected to stay FLAT and BOUNDED across
tiers -- a few hundred MB -- unlike the oracle's own full-frame residency,
which the baseline doc's staged attribution shows growing to 5.75 GB at the
3,000,000-row tier. That contrast (flat native vs. growing oracle) is this
slice's headline memory win, and this script's job is to measure it.

Usage:
  # 1. Build one tier's on-disk source (identical to bench_c1_oracle.py's
  #    own build-data; reuse that command directly, or let `drive` do it).
  python bench_c1_oracle.py build-data <n_rows> <data_dir>

  # 2. One fresh-process native run (what `drive` spawns; useful standalone):
  python bench_c1_native.py run-native <data_dir> [chunk_rows]

  # 3. Orchestrate calibration/measurement for a list of tiers, native AND a
  #    same-tier oracle comparison run (needed at tiers the baseline doc did
  #    not already measure, e.g. the 1,000,000-row moderate tier):
  python bench_c1_native.py drive --tiers 10000,1000000 --out results.json

Safety (12 GiB box): every child is polled for VmHWM and killed the instant
it crosses `ABORT_RSS_KB` (reused from `bench_c1_oracle.py`), same as the
oracle bench. Do NOT pass a tier at or above 3,000,000 to this driver's
oracle-comparison leg: that is Task 3.0's own already-measured tier (5.75 GB,
close to the box's limits); this script's job is the SMALLER tiers the
baseline doc did not already cover for a same-run comparison.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench_c1_oracle as oracle  # noqa: E402 -- sibling script, path set above


def _run_fresh_this_script(args: list[str]) -> tuple[dict[str, Any], int]:
    """Same fresh-process + external-VmHWM-polling contract as
    `bench_c1_oracle.py`'s own `_run_fresh` (reusing its polling loop,
    `_read_vmhwm_kb`, and `ABORT_RSS_KB` safety net directly), but targeting
    THIS script (`bench_c1_native.py`) rather than the oracle one -- needed
    for the `run-native` subcommand, which only exists here.
    """
    cmd = [sys.executable, str(HERE / "bench_c1_native.py"), *args]
    proc = subprocess.Popen(  # noqa: S603 fixed local benchmark command, no untrusted input
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    peak_rss_kb = 0
    aborted = False
    while proc.poll() is None:
        hwm = oracle._read_vmhwm_kb(proc.pid)
        if hwm is not None and hwm > peak_rss_kb:
            peak_rss_kb = hwm
        if peak_rss_kb > oracle.ABORT_RSS_KB:
            proc.kill()
            proc.wait()
            aborted = True
            break
        time.sleep(0.02)
    if not aborted:
        hwm = oracle._read_vmhwm_kb(proc.pid)
        if hwm is not None and hwm > peak_rss_kb:
            peak_rss_kb = hwm
    stdout, stderr = proc.communicate()
    if aborted:
        raise RuntimeError(
            f"ABORTED: child crossed ABORT_RSS_KB={oracle.ABORT_RSS_KB} during {args}; "
            f"peak observed={peak_rss_kb}kB. Safety kill, not an engine crash.\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
    if proc.returncode != 0:
        raise RuntimeError(f"worker failed (rc={proc.returncode}) for {args}:\n{stdout}\n{stderr}")
    m = oracle._JSON_RE.search(stdout)
    if not m:
        raise RuntimeError(f"no BENCH_JSON in worker stdout for {args}:\n{stdout}")
    return json.loads(m.group(1)), peak_rss_kb


# The frozen JC-3 chunk size (PHASE3-C1-BASELINE.md); the native route's own
# streaming granularity, distinct from the oracle's unchunked full-frame run.
DEFAULT_CHUNK_ROWS = 50_000

# Frozen recipe shape (PHASE3-C1-BASELINE.md): 3 faker + 6 hash columns on
# `patients`, 2 hash columns on `observations`. Used only to compute a
# rows/s/col throughput figure comparable to the oracle's own metric
# definition (`bench_c1_oracle.py`'s `hash_tput_median_rows_s` /
# `faker_tput_median_rows_s`); never fed into the actual masking config.
_HASH_COLS_TOTAL = 8
_FAKER_COLS_TOTAL = 3


def _stream_parquet(path: Path, batch_rows: int) -> Any:
    """Lazy per-batch Parquet reader, mirroring `bench_worker_native.py`'s own
    `_stream_parquet`: the process this feeds never holds more than one
    input batch at a time, so its peak RSS measures the STREAMING route, not
    whole-table residency. Reading the file with `pq.read_table` first (an
    earlier version of this function did) would inflate peak RSS with the
    full table regardless of `chunk_rows`, defeating the point of this bench.
    """
    for record_batch in pq.ParquetFile(path).iter_batches(batch_size=batch_rows):
        yield pa.Table.from_batches([record_batch])


def run_native(data_dir: Path, chunk_rows: int = DEFAULT_CHUNK_ROWS) -> dict[str, Any]:
    """Run the native+streaming route end to end over both tables, in THIS
    process (the caller measures RSS externally, mirroring `run_stage` in
    `bench_c1_oracle.py`).

    Streams each table LAZILY (`_stream_parquet`, one `chunk_rows`-row batch
    resident at a time) into `run_native_or_oracle_chunked`, one call per
    table, sharing one `PoolCache` (mirrors a real coordinator processing
    every table in a job against one cache). Aborts loudly if either table
    reroutes to the oracle: this script measures the NATIVE route only, and
    a silent fallback here would misreport oracle performance as native.

    `patients`' pool_quality pair files are written INCREMENTALLY, one
    (source_chunk, masked_chunk) pair at a time via `zip(..., strict=True)`
    over a SECOND lazy read of the same file (the first is already consumed
    by the dispatch call): the strict zip also proves the two streams stay
    in lockstep (same chunk count, same row counts), the same 1:1
    correspondence the gate test's route ledger checks.
    """
    from decoy_engine.execution.native._dispatch import (
        NativeRouteEvidence,
        run_native_or_oracle_chunked,
    )
    from decoy_engine.generation.pool import PoolCache
    from decoy_engine.keyprovider import SecretKeyProvider

    config = oracle.build_config(data_dir)
    key_provider = SecretKeyProvider(secret=oracle.FIXED_MASK_KEY, key_version="v1")
    pool_cache = PoolCache()

    evidences: dict[str, NativeRouteEvidence] = {}
    n_rows = 0
    pool_dup: dict[str, dict[str, Any]] = {}
    t0 = time.perf_counter()
    for table in ("patients", "observations"):
        path = data_dir / f"{table}.parquet"
        n_rows = pq.ParquetFile(path).metadata.num_rows
        sink: list[NativeRouteEvidence] = []
        masked_chunks = run_native_or_oracle_chunked(
            config,
            _stream_parquet(path, chunk_rows),
            table=table,
            engine_version="phase3-c1-native-bench",
            key_provider=key_provider,
            route_evidence_sink=sink,
            pool_cache=pool_cache,
        )
        # `route_evidence_sink` is populated EAGERLY (before any chunk is
        # masked -- `run_native_or_oracle_chunked`'s own docstring), so this
        # check runs BEFORE consuming `masked_chunks` below: a reroute is
        # caught without first paying for the oracle fallback's full
        # computation.
        evidence = sink[0]
        if not evidence.native_admitted:
            raise RuntimeError(
                f"bench_c1_native: table {table!r} rerouted to the oracle "
                f"({evidence.reroute_reason}); this bench measures the native "
                "route only, so a fallback here would misreport its numbers"
            )
        evidences[table] = evidence

        if table == "patients":
            # Pair files for the SAME out-of-band pool_quality aggregation
            # the oracle worker writes (bench_c1_oracle.py's own
            # `_compute_pool_quality`), so this script's pool_quality numbers
            # are computed by the identical frozen method, never a
            # native-specific re-derivation. Written incrementally (one
            # ParquetWriter per column, opened lazily on the first chunk) so
            # this loop never accumulates the whole masked table either.
            pair_writers: dict[str, pq.ParquetWriter] = {}
            for source_chunk, masked_chunk in zip(
                _stream_parquet(path, chunk_rows), masked_chunks, strict=True
            ):
                for col in oracle.FAKER_COLUMNS:
                    pairs = pa.table(
                        {"source": source_chunk.column(col), "masked": masked_chunk.column(col)}
                    )
                    writer = pair_writers.get(col)
                    if writer is None:
                        writer = pq.ParquetWriter(
                            str(data_dir / f"pairs_{col}.parquet"), pairs.schema
                        )
                        pair_writers[col] = writer
                    writer.write_table(pairs)
            for writer in pair_writers.values():
                writer.close()
            pool_dup = _pool_duplicate_counts(config, key_provider)
        else:
            for _masked_chunk in masked_chunks:
                pass  # drop immediately; never accumulate (bench_worker_native.py's convention)
    wall_s = time.perf_counter() - t0

    patients_ev = evidences["patients"]
    observations_ev = evidences["observations"]
    hash_elapsed = patients_ev.kernel_elapsed_s.get(
        "hash", 0.0
    ) + observations_ev.kernel_elapsed_s.get("hash", 0.0)
    faker_elapsed = patients_ev.kernel_elapsed_s.get("faker", 0.0)
    hash_tput = (n_rows * _HASH_COLS_TOTAL) / hash_elapsed if hash_elapsed else None
    faker_tput = (n_rows * _FAKER_COLS_TOTAL) / faker_elapsed if faker_elapsed else None

    return {
        "wall_s": wall_s,
        "n_rows": n_rows,
        "hash_tput_rows_s": hash_tput,
        "faker_tput_rows_s": faker_tput,
        "pool_select_calls": patients_ev.pool_select_calls,
        "pool_select_executed": patients_ev.pool_select_executed,
        "hash_kernel_calls": (
            patients_ev.kernel_calls.get("hash", 0) + observations_ev.kernel_calls.get("hash", 0)
        ),
        "compiled_kernel_executed": (
            patients_ev.compiled_kernel_executed and observations_ev.compiled_kernel_executed
        ),
        "native_admitted": {
            "patients": patients_ev.native_admitted,
            "observations": observations_ev.native_admitted,
        },
        "pool_duplicate": pool_dup,
    }


def _pool_duplicate_counts(config: dict[str, Any], key_provider: Any) -> dict[str, dict[str, Any]]:
    """Same frozen pool-duplicate metric as `bench_c1_oracle.py`'s own
    `_pool_duplicate_counts`, rebuilt here identically (the native route's
    pools are byte-identical to the oracle's own for the same identity --
    Task 3.1's HIGH 1 shared-identity guarantee -- so this is a bounded,
    O(pool_size) rebuild-and-count, not a second measurement method)."""
    from decoy_engine.generation.pool._builder import PoolBuilder
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._seed import _normalize_job_seed_int
    from decoy_engine.profile import profile_source

    job_seed_int = _normalize_job_seed_int(config)
    profile = profile_source(config, seed=job_seed_int)
    plan = compile_plan(config, profile, decoy_engine_version="phase3-c1-native-bench")
    from decoy_engine.keyprovider import require_mask_key, resolve_key_provider

    resolved = resolve_key_provider(plan=plan, key_provider=key_provider, mask_secret_ref=None)
    require_mask_key(plan, resolved)  # validated for parity with the oracle path; unused here

    from decoy_engine.providers_v2 import get_default_registry

    builder = PoolBuilder(get_default_registry())
    table_seed = next(ts for name, ts in plan.seed_envelope.per_table if name == "patients")
    out: dict[str, dict[str, Any]] = {}
    for col_name, col_seed in table_seed.per_column:
        if col_seed.strategy != "faker":
            continue
        # `provider` and `pool_size` are `str | None` / `int | None` on
        # `ColumnSeed` generally (only faker columns set them), but the C1
        # phase3 eligibility admission already requires both to be resolved
        # for every faker column that reached the native route (JC-5); a
        # None here would mean this bench ran against a config admission
        # should have rejected.
        if col_seed.provider is None:
            raise AssertionError(f"{col_name}: admitted faker column has no provider")
        if col_seed.pool_size is None:
            raise AssertionError(f"{col_name}: admitted faker column has no pool_size")
        pool = builder.build(
            provider=col_seed.provider,
            size=col_seed.pool_size,
            job_seed=plan.seed_envelope.job_seed,
            locale=None,
            config={},
            namespace=col_seed.namespace,
        )
        dup = pool.size - pool.distinct_count
        out[col_name] = {
            "pool_size": pool.size,
            "distinct_pool_values": pool.distinct_count,
            "pool_duplicate_count": dup,
            "pool_duplicate_rate": dup / pool.size if pool.size else 0.0,
        }
    return out


def drive(
    tiers: list[int],
    reps: int,
    warmup: int,
    out_path: Path,
    data_root: Path,
    chunk_rows: int,
    *,
    run_oracle_comparison: bool,
) -> dict[str, Any]:
    all_results: dict[str, Any] = {}
    for n_rows in tiers:
        sys.stderr.write(f"\n=== TIER {n_rows} rows (native) ===\n")
        sys.stderr.flush()
        data_dir = data_root / f"tier_{n_rows}"
        sys.stderr.write("  building data (out of band, reusing bench_c1_oracle.build_data) ...\n")
        sys.stderr.flush()
        oracle.build_data(n_rows, data_dir)

        for w in range(warmup):
            sys.stderr.write(f"  native warmup {w + 1}/{warmup} ...\n")
            sys.stderr.flush()
            _run_fresh_this_script(["run-native", str(data_dir), str(chunk_rows)])

        native_recs = []
        native_peaks = []
        for i in range(reps):
            rec, peak = _run_fresh_this_script(["run-native", str(data_dir), str(chunk_rows)])
            native_recs.append(rec)
            native_peaks.append(peak)
            sys.stderr.write(
                f"  native rep {i + 1}/{reps}: wall={rec['wall_s']:.2f}s rss={peak}kB\n"
            )
            sys.stderr.flush()

        native_walls = sorted(r["wall_s"] for r in native_recs)
        q = (
            statistics.quantiles(native_walls, n=4, method="inclusive")
            if len(native_walls) >= 2
            else [native_walls[0]] * 3
        )
        pool_quality = oracle._compute_pool_quality(data_dir, native_recs[-1]["pool_duplicate"])

        summary: dict[str, Any] = {
            "n_rows": n_rows,
            "chunk_rows": chunk_rows,
            "reps": len(native_recs),
            "native_wall_median_s": statistics.median(native_walls),
            "native_wall_iqr_s": q[2] - q[0],
            "native_peak_rss_kb": max(native_peaks),
            "native_hash_tput_median_rows_s": _median_or_none(
                [r["hash_tput_rows_s"] for r in native_recs if r.get("hash_tput_rows_s")]
            ),
            "native_faker_tput_median_rows_s": _median_or_none(
                [r["faker_tput_rows_s"] for r in native_recs if r.get("faker_tput_rows_s")]
            ),
            "route_ledger": {
                "pool_select_calls": native_recs[-1]["pool_select_calls"],
                "pool_select_executed": native_recs[-1]["pool_select_executed"],
                "hash_kernel_calls": native_recs[-1]["hash_kernel_calls"],
                "compiled_kernel_executed": native_recs[-1]["compiled_kernel_executed"],
                "native_admitted": native_recs[-1]["native_admitted"],
            },
            "pool_quality": pool_quality,
        }

        if run_oracle_comparison:
            sys.stderr.write(
                f"  oracle comparison run at tier {n_rows} (same-run, apples to apples) ...\n"
            )
            sys.stderr.flush()
            oracle_rec, oracle_peak = oracle._run_fresh(["run-stage", "publication", str(data_dir)])
            summary["oracle_wall_s"] = oracle_rec["wall_s"]
            summary["oracle_peak_rss_kb"] = oracle_peak
            summary["wall_ratio_native_over_oracle"] = (
                summary["native_wall_median_s"] / oracle_rec["wall_s"]
                if oracle_rec["wall_s"]
                else None
            )

        all_results[str(n_rows)] = summary
        sys.stderr.write(
            f"  SUMMARY n={n_rows}: native_wall_median={summary['native_wall_median_s']:.2f}s "
            f"native_peak_rss={summary['native_peak_rss_kb'] / 1000:.1f}MB\n"
        )
        sys.stderr.flush()
        out_path.write_text(json.dumps(all_results, indent=2))
    return all_results


def _median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_native = sub.add_parser("run-native")
    p_native.add_argument("data_dir", type=Path)
    p_native.add_argument("chunk_rows", type=int, nargs="?", default=DEFAULT_CHUNK_ROWS)

    p_drive = sub.add_parser("drive")
    p_drive.add_argument("--tiers", required=True, help="comma-separated row counts")
    p_drive.add_argument("--reps", type=int, default=5)
    p_drive.add_argument("--warmup", type=int, default=1)
    p_drive.add_argument("--chunk-rows", type=int, default=DEFAULT_CHUNK_ROWS)
    p_drive.add_argument("--out", required=True, type=Path)
    p_drive.add_argument("--data-root", type=Path, default=HERE / "c1_data")
    p_drive.add_argument(
        "--skip-oracle-comparison",
        action="store_true",
        help="skip the same-run oracle comparison leg (use a tier's frozen "
        "PHASE3-C1-BASELINE.md numbers instead of re-measuring)",
    )

    args = ap.parse_args()
    if args.cmd == "run-native":
        rec = run_native(args.data_dir, args.chunk_rows)
        print("BENCH_JSON " + json.dumps(rec))
        return
    if args.cmd == "drive":
        tiers = [int(x) for x in args.tiers.split(",")]
        results = drive(
            tiers,
            args.reps,
            args.warmup,
            args.out,
            args.data_root,
            args.chunk_rows,
            run_oracle_comparison=not args.skip_oracle_comparison,
        )
        print(json.dumps(results, indent=2))
        return


if __name__ == "__main__":
    main()
