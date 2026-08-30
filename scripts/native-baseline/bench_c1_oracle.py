"""Deterministic C1 oracle baseline (Task 3.0): staged RSS + pool_quality.

Builds the frozen deterministic C1 recipe's two-table dataset out of band,
runs it on the PINNED pandas full-frame oracle (`substrate="pandas"`,
`execution_mode="full_frame"`, `auto_chunk=False`), and attributes peak RSS
into four buckets (input load; pool build; selection; publication) via
FRESH-PROCESS PREFIX runs, per docs/plans/PHASE3-C1-BASELINE.md.

`VmHWM` is a process-lifetime high-water mark, so one process cannot yield
independent per-stage peaks. Each prefix stage below is a FRESH process that
redoes every earlier stage's work and then stops; the parent polls the
child's `/proc/<pid>/status` VmHWM externally (same method as
`bench_driver.py`), and a stage's own contribution is the difference between
consecutive prefix high-water marks. The last prefix ("publication") runs the
real oracle end to end, so its own VmHWM doubles as the total.

`pool_build` and `selection` call the SAME production functions the real
route uses (`_warm_faker_pools`, `FakerStrategyHandler.run`) rather than
reimplementing pool identity or sampling, so a staged prefix cannot silently
drift from what `run_pipeline` actually does.

The `pool_quality` collision aggregation runs OUT OF BAND, after the
publication stage's process has already exited (never inside a measured
process), and stays bounded via DuckDB's own spilling hash aggregation
(a `memory_limit` + `temp_directory` pragma) rather than a Python-side
`set()` over the (potentially large) distinct-source population.

Usage:
  # 1. Build one tier's on-disk source (separate, non-timed, out-of-band step):
  python bench_c1_oracle.py build-data <n_rows> <data_dir> [batch_rows]

  # 2. One fresh-process stage prefix (what `drive` spawns; useful standalone
  #    for debugging one stage):
  python bench_c1_oracle.py run-stage <stage> <data_dir>

  # 3. Orchestrate calibration/measurement for a list of tiers:
  python bench_c1_oracle.py drive --tiers 10000,400000 --out results.json

Safety (12 GiB box): every child is polled for VmHWM and killed the instant
it crosses `ABORT_RSS_KB` (see the constant below), well under the box's
capacity, rather than being allowed to run to an OOM.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# ---- Frozen C1 constants (docs/plans/PHASE3-C1-BASELINE.md) --------------
FIXED_SEED = 20260830
FIXED_MASK_KEY = bytes.fromhex("b7c3d9e1f5a2846913c0d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7")
POOL_SIZE = 10_000

NS_FIRST = "first_name_identity"
NS_LAST = "last_name_identity"
NS_MAIDEN = "maiden_name_identity"

# Bounded distinct-source pools the synthetic data cycles through (fixed
# regardless of row count, so distinct-source counts -- and therefore
# pool_quality rates -- are a property of (distinct_sources, pool_size),
# not of the tier's row count; see the baseline doc's tier-design note).
N_FIRST_DISTINCT = 1_000
N_LAST_DISTINCT = 1_200
N_MAIDEN_DISTINCT = 900
MAIDEN_NULL_EVERY = 5  # ~60% null (ids % 5 < 3): most patients have no maiden name.
DEATHDATE_NULL_EVERY = 10  # ~90% null: most synthetic patients are alive.

STAGES = ("input_load", "pool_build", "selection", "publication")

# Hard safety ceiling: kill a child the instant its VmHWM crosses this, well
# under the 12 GiB box, rather than letting a miscalibrated tier run to OOM.
ABORT_RSS_KB = 7_000_000  # ~6.8 GiB

_JSON_RE = re.compile(r"^BENCH_JSON (.*)$", re.MULTILINE)
_HWM_RE = re.compile(r"^VmHWM:\s*(\d+)\s*kB", re.MULTILINE)

HERE = Path(__file__).resolve().parent


# ---- Out-of-band data generation ------------------------------------------


def _patients_batch(start: int, end: int) -> pa.Table:
    """One [start, end) row-range slice of the frozen patients shape.

    Vectorized numpy so generation stays O(end - start) memory and cheap
    relative to the masking pass it feeds.
    """
    ids = np.arange(start, end, dtype=np.int64)
    ids_str = ids.astype("U")

    first_src = np.char.add("First", np.mod(ids, N_FIRST_DISTINCT).astype("U"))
    last_src = np.char.add("Last", np.mod(ids, N_LAST_DISTINCT).astype("U"))
    maiden_src = np.char.add("Maiden", np.mod(ids, N_MAIDEN_DISTINCT).astype("U")).astype(object)
    maiden_src[np.mod(ids, MAIDEN_NULL_EVERY) < 3] = None

    ssn = np.char.add(np.char.add("SSN", np.mod(ids, 900).astype("U")), "-00-0000")
    drivers = np.char.add("DL", ids_str)
    passport = np.char.add("PP", ids_str)
    address = np.char.add(np.char.add("Addr", ids_str), np.mod(ids, 50).astype("U"))

    base_birth = np.datetime64("1930-01-01")
    birthdate = (base_birth + np.mod(ids, 30_000).astype("timedelta64[D]")).astype(str)
    deathdate = (
        (base_birth + np.mod(ids, 30_000).astype("timedelta64[D]")).astype(str).astype(object)
    )
    deathdate[np.mod(ids, DEATHDATE_NULL_EVERY) != 0] = None

    return pa.table(
        {
            "FIRST": pa.array(first_src, type=pa.string()),
            "LAST": pa.array(last_src, type=pa.string()),
            "MAIDEN": pa.array(maiden_src, type=pa.string()),
            "SSN": pa.array(ssn, type=pa.string()),
            "DRIVERS": pa.array(drivers, type=pa.string()),
            "PASSPORT": pa.array(passport, type=pa.string()),
            "ADDRESS": pa.array(address, type=pa.string()),
            "BIRTHDATE": pa.array(birthdate, type=pa.string()),
            "DEATHDATE": pa.array(deathdate, type=pa.string()),
        }
    )


def _observations_batch(start: int, end: int) -> pa.Table:
    """One [start, end) row-range slice of the frozen observations shape."""
    ids = np.arange(start, end, dtype=np.int64)
    base_date = np.datetime64("2015-01-01")
    date = (base_date + np.mod(ids, 3_650).astype("timedelta64[D]")).astype(str)
    value = np.char.add("val_", np.mod(ids, 5_000).astype("U"))
    return pa.table(
        {
            "DATE": pa.array(date, type=pa.string()),
            "VALUE": pa.array(value, type=pa.string()),
        }
    )


_BATCH_BUILDERS = {"patients": _patients_batch, "observations": _observations_batch}
DEFAULT_BATCH_ROWS = 50_000

# Stage prefix order: each stage's fresh-process run does strictly more work
# than the previous, so its VmHWM high-water mark should be >= the previous.
_STAGE_ORDER = ("input_load", "pool_build", "selection", "publication")


def _stage_deltas(stage_peaks: dict[str, int]) -> dict[str, int]:
    """Per-stage RSS attribution as consecutive prefix-peak differences.

    Prefix peaks are expected monotonic non-decreasing. A negative delta means
    a noisy prefix run made a later stage's high-water mark LOWER than an
    earlier one; clamp it to 0 and warn loudly rather than silently reporting a
    nonsense (negative) attribution. Observed runs are cleanly monotonic; this
    is a guard against future noise, not a correction applied today.
    """
    out: dict[str, int] = {"input_load": stage_peaks["input_load"]}
    for prev, cur in itertools.pairwise(_STAGE_ORDER):
        delta = stage_peaks[cur] - stage_peaks[prev]
        if delta < 0:
            sys.stderr.write(
                f"  WARN non-monotonic prefix peak: {cur} ({stage_peaks[cur]} kB) "
                f"< {prev} ({stage_peaks[prev]} kB); clamping {cur}_delta to 0\n"
            )
            delta = 0
        out[f"{cur}_delta"] = delta
    return out


def build_data(n_rows: int, data_dir: Path, batch_rows: int = DEFAULT_BATCH_ROWS) -> None:
    """Write both tables' frozen shape to on-disk Parquet, in BATCHES.

    Run ONCE per tier, in the driver (unmeasured) process, batched to disk:
    each batch is written as its own row group and dropped, so generation
    never inflates a MEASURED worker's RSS (the measured stage workers are
    separate `_run_fresh` processes that re-read these files from disk; the
    driver itself is never a measured process).
    Both tables get the SAME row count (a deliberate simplification recorded
    in the baseline doc -- Decision 5's C1 shape does not mandate a
    patients:observations ratio, and equal counts still stress the intended
    full-frame-residency + per-column sampler-temporaries hypothesis).
    Also writes a small (<=2,000-row) profiling-sample CSV per table, since
    `profile_source` reads the config-declared file path independently of
    the full in-memory source the measured worker constructs (same split as
    `bench_worker.py`).
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    for table, builder in _BATCH_BUILDERS.items():
        out_path = data_dir / f"{table}.parquet"
        writer: pq.ParquetWriter | None = None
        sample_written = False
        for start in range(0, max(n_rows, 1), batch_rows):
            end = min(start + batch_rows, n_rows)
            if end <= start:
                continue
            batch = builder(start, end)
            if writer is None:
                writer = pq.ParquetWriter(str(out_path), batch.schema)
            writer.write_table(batch)
            if not sample_written:
                sample_n = min(batch.num_rows, 2_000)
                batch.slice(0, sample_n).to_pandas().to_csv(
                    data_dir / f"{table}_sample.csv", index=False
                )
                sample_written = True
        if writer is not None:
            writer.close()
        else:
            empty = builder(0, 0)
            pq.write_table(empty, str(out_path))
            empty.to_pandas().to_csv(data_dir / f"{table}_sample.csv", index=False)


# ---- The frozen deterministic C1 config -----------------------------------


def build_config(data_dir: Path) -> dict[str, Any]:
    """The deterministic C1 recipe variant (frozen; see the baseline doc).

    Same two-table shape as the platform's `mask-fullframe-saturate`
    scenario, with `deterministic: true` + an explicit `namespace` + an
    explicit `pool_size` added to the three faker columns (JC-5): the
    minimum needed to satisfy `_chunked.py`'s chunk-admission conditions,
    frozen here even though this baseline runs the PINNED oracle, not the
    (not-yet-built) native route, so Task 3.1 reproduces the identical
    config.
    """
    from decoy_engine.config._pipeline import PipelineConfig

    raw = {
        "version": 1,
        "global_settings": {"seed": FIXED_SEED, "post_validation": False},
        "sources": {
            "patients": {
                "type": "file",
                "format": "csv",
                "path": str(data_dir / "patients_sample.csv"),
            },
            "observations": {
                "type": "file",
                "format": "csv",
                "path": str(data_dir / "observations_sample.csv"),
            },
        },
        "targets": {
            "patients": {
                "type": "file",
                "format": "csv",
                "path": str(data_dir / "patients_masked.csv"),
            },
            "observations": {
                "type": "file",
                "format": "csv",
                "path": str(data_dir / "observations_masked.csv"),
            },
        },
        "tables": [
            {
                "name": "patients",
                "columns": [
                    {
                        "name": "FIRST",
                        "strategy": "faker",
                        "provider": "person_first_name",
                        "deterministic": True,
                        "namespace": NS_FIRST,
                        "pool_size": POOL_SIZE,
                    },
                    {
                        "name": "LAST",
                        "strategy": "faker",
                        "provider": "person_last_name",
                        "deterministic": True,
                        "namespace": NS_LAST,
                        "pool_size": POOL_SIZE,
                    },
                    {
                        "name": "MAIDEN",
                        "strategy": "faker",
                        "provider": "person_last_name",
                        "deterministic": True,
                        "namespace": NS_MAIDEN,
                        "pool_size": POOL_SIZE,
                    },
                    {"name": "SSN", "strategy": "hash", "namespace": "ssn_identity"},
                    {"name": "DRIVERS", "strategy": "hash", "namespace": "drivers_identity"},
                    {"name": "PASSPORT", "strategy": "hash", "namespace": "passport_identity"},
                    {"name": "ADDRESS", "strategy": "hash", "namespace": "address_identity"},
                    {"name": "BIRTHDATE", "strategy": "hash", "namespace": "birthdate_identity"},
                    {"name": "DEATHDATE", "strategy": "hash", "namespace": "deathdate_identity"},
                ],
            },
            {
                "name": "observations",
                "columns": [
                    {
                        "name": "DATE",
                        "strategy": "hash",
                        "namespace": "observation_date_identity",
                    },
                    {
                        "name": "VALUE",
                        "strategy": "hash",
                        "namespace": "observation_value_identity",
                    },
                ],
            },
        ],
        "relationships": [],
        "namespaces": {},
    }
    return PipelineConfig.model_validate(raw).model_dump()


FAKER_COLUMNS = ("FIRST", "LAST", "MAIDEN")


# ---- Staged worker (invoked once per fresh process) ------------------------


def _load_sources(data_dir: Path) -> dict[str, pa.Table]:
    return {
        "patients": pq.read_table(data_dir / "patients.parquet"),
        "observations": pq.read_table(data_dir / "observations.parquet"),
    }


def _compiled_plan(config: dict[str, Any], key_provider: Any) -> tuple[Any, bytes, Any, Any]:
    """Compile (plan, mask_key, namespace_registry, relationship_graph),
    the same four objects `run_pipeline` builds, so a staged prefix's
    `pool_build`/`selection` work matches production exactly."""
    from decoy_engine.keyprovider import require_mask_key, resolve_key_provider
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._seed import _normalize_job_seed_int
    from decoy_engine.profile import profile_source
    from decoy_engine.relationships import RelationshipGraph, build_namespace_registry

    job_seed_int = _normalize_job_seed_int(config)
    profile = profile_source(config, seed=job_seed_int)
    plan = compile_plan(config, profile, decoy_engine_version="phase3-c1-baseline")
    resolved_key_provider = resolve_key_provider(
        plan=plan, key_provider=key_provider, mask_secret_ref=None
    )
    mask_key = require_mask_key(plan, resolved_key_provider)
    ns_registry = build_namespace_registry(config, profile)
    graph = RelationshipGraph(edges=(), ordering=())
    return plan, mask_key, ns_registry, graph


def _warm_pools(plan: Any, registry: Any) -> Any:
    """Stage 2: build the three faker pools via the SAME pre-warm function
    the chunked route uses (`_chunked._warm_faker_pools`), not a hand-rolled
    equivalent."""
    from decoy_engine.execution._chunked import _warm_faker_pools
    from decoy_engine.generation.pool._cache import PoolCache

    pool_cache = PoolCache()
    _warm_faker_pools(plan, table="patients", registry=registry, pool_cache=pool_cache)
    return pool_cache


def _run_selection(
    plan: Any, mask_key: bytes, ns_registry: Any, graph: Any, registry: Any, patients: pa.Table
) -> None:
    """Stage 3: pool build (Stage 2's work, redone in this fresh process)
    plus deterministic selection for FIRST/LAST/MAIDEN via the production
    `FakerStrategyHandler`, without touching any hash column."""
    from decoy_engine.execution._adapter import StrategyContext
    from decoy_engine.execution._fk_keys import to_pandas_fk_safe
    from decoy_engine.execution._strategies._faker import FakerStrategyHandler

    pool_cache = _warm_pools(plan, registry)
    df = to_pandas_fk_safe(patients, set())
    ctx = StrategyContext(
        registry=registry,
        pool_cache=pool_cache,
        relationship_graph=graph,
        namespace_registry=ns_registry,
        job_seed=plan.seed_envelope.job_seed,
        mask_key=mask_key,
    )
    object.__setattr__(ctx, "current_table", "patients")
    table_seed = next(ts for name, ts in plan.seed_envelope.per_table if name == "patients")
    handler = FakerStrategyHandler()
    for col_name, col_seed in table_seed.per_column:
        if col_seed.strategy == "faker":
            df, _warnings = handler.run(df, col_name, col_seed, ctx)


def _pool_duplicate_counts(plan: Any, registry: Any) -> dict[str, dict[str, Any]]:
    """Pool-duplicate count per faker column: `pool_size - |distinct_pool_values|`.

    Bounded by construction (`pool_size` is a small, explicit config knob,
    not a function of row count), so a direct rebuild-and-count needs no
    spill-backed aggregation the way the (potentially huge) distinct-source
    collision measurement does.
    """
    from decoy_engine.generation.pool._builder import PoolBuilder

    builder = PoolBuilder(registry)
    table_seed = next(ts for name, ts in plan.seed_envelope.per_table if name == "patients")
    out: dict[str, dict[str, Any]] = {}
    for col_name, col_seed in table_seed.per_column:
        if col_seed.strategy != "faker":
            continue
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


def run_stage(stage: str, data_dir: Path) -> dict[str, Any]:
    """Run every stage up to and including `stage`, in THIS process, then
    return a small JSON-able record. The caller measures RSS externally."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")

    t0 = time.perf_counter()
    sources = _load_sources(data_dir)
    if stage == "input_load":
        return {
            "stage": stage,
            "wall_s": time.perf_counter() - t0,
            "n_rows": sources["patients"].num_rows,
        }

    from decoy_engine.keyprovider import SecretKeyProvider
    from decoy_engine.providers_v2 import get_default_registry

    config = build_config(data_dir)
    key_provider = SecretKeyProvider(secret=FIXED_MASK_KEY, key_version="v1")
    registry = get_default_registry()
    plan, mask_key, ns_registry, graph = _compiled_plan(config, key_provider)

    if stage == "pool_build":
        _warm_pools(plan, registry)
        return {
            "stage": stage,
            "wall_s": time.perf_counter() - t0,
            "n_rows": sources["patients"].num_rows,
        }

    if stage == "selection":
        _run_selection(plan, mask_key, ns_registry, graph, registry, sources["patients"])
        return {
            "stage": stage,
            "wall_s": time.perf_counter() - t0,
            "n_rows": sources["patients"].num_rows,
        }

    # stage == "publication": the real oracle, end to end. This both re-does
    # (redundantly, in the SAME process) the work of every earlier stage AND
    # is the number the baseline reports as wall/throughput/pool_quality
    # source data, mirroring bench_worker.py's convention exactly.
    from decoy_engine.execution._pipeline import run_pipeline

    t_pipeline0 = time.perf_counter()
    result = run_pipeline(
        config,
        sources,
        engine_version="phase3-c1-baseline",
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        key_provider=key_provider,
        use_byte_estimate_routing=False,
        use_probe_routing=False,
    )
    wall_s = time.perf_counter() - t_pipeline0

    hash_ms = 0.0
    hash_cols = 0
    faker_ms = 0.0
    faker_cols = 0
    for rec_t in result.timings:
        if rec_t.strategy_type == "hash":
            hash_ms += rec_t.elapsed_ms
            hash_cols += 1
        elif rec_t.strategy_type == "faker":
            faker_ms += rec_t.elapsed_ms
            faker_cols += 1

    # Pair files for the OUT-OF-BAND pool_quality aggregation (never read
    # back inside this measured process): source vs masked output, per
    # faker column. Writing them costs only I/O over data already resident;
    # it does not add an O(distinct) structure to this process.
    masked = result.outputs["patients"]
    for col in FAKER_COLUMNS:
        pairs = pa.table({"source": sources["patients"].column(col), "masked": masked.column(col)})
        pq.write_table(pairs, data_dir / f"pairs_{col}.parquet")

    pool_dup = _pool_duplicate_counts(plan, registry)

    return {
        "stage": stage,
        "wall_s": wall_s,
        "n_rows": sources["patients"].num_rows,
        "out_rows": masked.num_rows,
        "hash_ms": hash_ms,
        "hash_cols": hash_cols,
        "faker_ms": faker_ms,
        "faker_cols": faker_cols,
        "pool_duplicate": pool_dup,
    }


# ---- External VmHWM polling (mirrors bench_driver.py) ----------------------


def _read_vmhwm_kb(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as fh:
            m = _HWM_RE.search(fh.read())
            return int(m.group(1)) if m else None
    except (FileNotFoundError, ProcessLookupError, OSError):
        return None


def _run_fresh(args: list[str]) -> tuple[dict[str, Any], int]:
    """Run one fresh-process worker invocation; return (record, peak_rss_kb).

    Polls the child's VmHWM externally (never read from inside the measured
    process) and kills it immediately if it crosses `ABORT_RSS_KB` -- the
    hard safety net for the 12 GiB box.
    """
    cmd = [sys.executable, str(HERE / "bench_c1_oracle.py"), *args]
    proc = subprocess.Popen(  # noqa: S603 fixed local benchmark command, no untrusted input
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    peak_rss_kb = 0
    aborted = False
    while proc.poll() is None:
        hwm = _read_vmhwm_kb(proc.pid)
        if hwm is not None and hwm > peak_rss_kb:
            peak_rss_kb = hwm
        if peak_rss_kb > ABORT_RSS_KB:
            proc.kill()
            proc.wait()
            aborted = True
            break
        time.sleep(0.02)
    if not aborted:
        hwm = _read_vmhwm_kb(proc.pid)
        if hwm is not None and hwm > peak_rss_kb:
            peak_rss_kb = hwm
    stdout, stderr = proc.communicate()
    if aborted:
        raise RuntimeError(
            f"ABORTED: child crossed ABORT_RSS_KB={ABORT_RSS_KB} during {args}; "
            f"peak observed={peak_rss_kb}kB. Safety kill, not an engine crash.\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
    if proc.returncode != 0:
        raise RuntimeError(f"worker failed (rc={proc.returncode}) for {args}:\n{stdout}\n{stderr}")
    m = _JSON_RE.search(stdout)
    if not m:
        raise RuntimeError(f"no BENCH_JSON in worker stdout for {args}:\n{stdout}")
    return json.loads(m.group(1)), peak_rss_kb


def drive(
    tiers: list[int], reps: int, warmup: int, rss_reps: int, out_path: Path, data_root: Path
) -> dict[str, Any]:
    all_results: dict[str, Any] = {}
    for n_rows in tiers:
        sys.stderr.write(f"\n=== TIER {n_rows} rows ===\n")
        sys.stderr.flush()
        data_dir = data_root / f"tier_{n_rows}"
        sys.stderr.write("  building data (out of band) ...\n")
        sys.stderr.flush()
        build_data(n_rows, data_dir)

        stage_peaks: dict[str, int] = {}
        for stage in ("input_load", "pool_build", "selection"):
            peaks = []
            for _ in range(rss_reps):
                _rec, peak = _run_fresh(["run-stage", stage, str(data_dir)])
                peaks.append(peak)
            stage_peaks[stage] = max(peaks)
            sys.stderr.write(
                f"  stage={stage} peak_rss={stage_peaks[stage]}kB (max of {rss_reps})\n"
            )
            sys.stderr.flush()

        for w in range(warmup):
            sys.stderr.write(f"  publication warmup {w + 1}/{warmup} ...\n")
            sys.stderr.flush()
            _run_fresh(["run-stage", "publication", str(data_dir)])

        pub_recs = []
        pub_peaks = []
        for i in range(reps):
            rec, peak = _run_fresh(["run-stage", "publication", str(data_dir)])
            pub_recs.append(rec)
            pub_peaks.append(peak)
            sys.stderr.write(
                f"  publication rep {i + 1}/{reps}: wall={rec['wall_s']:.2f}s rss={peak}kB\n"
            )
            sys.stderr.flush()
        stage_peaks["publication"] = max(pub_peaks)

        pool_quality = _compute_pool_quality(data_dir, pub_recs[-1]["pool_duplicate"])

        walls = sorted(r["wall_s"] for r in pub_recs)
        q = (
            statistics.quantiles(walls, n=4, method="inclusive")
            if len(walls) >= 2
            else [walls[0]] * 3
        )
        hash_tputs = [
            n_rows / ((r["hash_ms"] / r["hash_cols"]) / 1000.0)
            for r in pub_recs
            if r.get("hash_ms") and r.get("hash_cols")
        ]
        faker_tputs = [
            n_rows / ((r["faker_ms"] / r["faker_cols"]) / 1000.0)
            for r in pub_recs
            if r.get("faker_ms") and r.get("faker_cols")
        ]
        summary = {
            "n_rows": n_rows,
            "reps": len(pub_recs),
            "wall_median_s": statistics.median(walls),
            "wall_iqr_s": q[2] - q[0],
            "hash_tput_median_rows_s": statistics.median(hash_tputs) if hash_tputs else None,
            "faker_tput_median_rows_s": statistics.median(faker_tputs) if faker_tputs else None,
            "stage_peak_rss_kb": stage_peaks,
            "stage_attribution_kb": _stage_deltas(stage_peaks),
            # max, not stage_peaks["publication"]: robust if a noisy prefix run
            # ever makes an earlier stage's high-water mark the largest.
            "total_peak_rss_kb": max(stage_peaks.values()),
            "pool_quality": pool_quality,
        }
        all_results[str(n_rows)] = summary
        sys.stderr.write(
            f"  SUMMARY n={n_rows}: wall_median={summary['wall_median_s']:.2f}s "
            f"total_peak_rss={summary['total_peak_rss_kb'] / 1024:.0f}MB\n"
        )
        sys.stderr.flush()
        out_path.write_text(json.dumps(all_results, indent=2))
    return all_results


def _compute_pool_quality(
    data_dir: Path, pool_duplicate: dict[str, dict[str, int]]
) -> dict[str, Any]:
    """Frozen pool_quality aggregation (docs/plans/PHASE3-C1-BASELINE.md):
    distinct-source collision rate via a BOUNDED (spill-backed) DuckDB
    group-by, plus the already-bounded pool-duplicate rate. Runs entirely
    out of band, after the publication process that wrote the pair files
    has already exited.
    """
    import duckdb

    spill_dir = data_dir / "duckdb_spill"
    spill_dir.mkdir(exist_ok=True)
    con = duckdb.connect()
    # Concrete memory limit + temp_directory: the group-by spills to disk
    # rather than materializing an O(distinct sources) hash table in RAM,
    # regardless of how many distinct source rows the tier holds.
    con.execute("PRAGMA memory_limit='512MB'")
    con.execute(f"PRAGMA temp_directory='{spill_dir}'")

    out: dict[str, Any] = {}
    for col in FAKER_COLUMNS:
        pairs_path = data_dir / f"pairs_{col}.parquet"
        row = con.execute(
            f"""
            WITH per_source AS (
                SELECT source,
                       ANY_VALUE(masked) AS out_val,
                       COUNT(DISTINCT masked) AS n_distinct_masked
                FROM read_parquet('{pairs_path}')
                WHERE source IS NOT NULL
                GROUP BY source
            )
            SELECT
                COUNT(*) AS distinct_sources,
                COUNT(DISTINCT out_val) AS distinct_outputs_for_distinct_sources,
                SUM(CASE WHEN n_distinct_masked > 1 THEN 1 ELSE 0 END) AS non_deterministic_sources
            FROM per_source
            """
        ).fetchone()
        if row is None:
            raise RuntimeError(f"pool_quality aggregation returned no row for column {col!r}")
        distinct_sources, distinct_outputs, non_deterministic = row
        if distinct_sources == 0:
            # Empty population (e.g. an all-null column): rate 0, pass, per
            # the frozen definition -- never silently omitted.
            collision_count = 0
            collision_rate = 0.0
        else:
            collision_count = distinct_sources - distinct_outputs
            collision_rate = collision_count / distinct_sources
        out[col] = {
            "distinct_sources": distinct_sources,
            "distinct_outputs_for_distinct_sources": distinct_outputs,
            "non_deterministic_sources": non_deterministic,  # must be 0 (QC, not the metric itself)
            "collision_count": collision_count,
            "collision_rate": collision_rate,
            "unique_feasibility": "N/A (reuse-only C1 scope)",
            **pool_duplicate.get(col, {}),
        }
    con.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build-data")
    p_build.add_argument("n_rows", type=int)
    p_build.add_argument("data_dir", type=Path)
    p_build.add_argument("batch_rows", type=int, nargs="?", default=DEFAULT_BATCH_ROWS)

    p_stage = sub.add_parser("run-stage")
    p_stage.add_argument("stage", choices=STAGES)
    p_stage.add_argument("data_dir", type=Path)

    p_drive = sub.add_parser("drive")
    p_drive.add_argument("--tiers", required=True, help="comma-separated row counts")
    p_drive.add_argument("--reps", type=int, default=5)
    p_drive.add_argument("--warmup", type=int, default=1)
    p_drive.add_argument("--rss-reps", type=int, default=3)
    p_drive.add_argument("--out", required=True, type=Path)
    p_drive.add_argument("--data-root", type=Path, default=HERE / "c1_data")

    args = ap.parse_args()
    if args.cmd == "build-data":
        build_data(args.n_rows, args.data_dir, args.batch_rows)
        return
    if args.cmd == "run-stage":
        rec = run_stage(args.stage, args.data_dir)
        print("BENCH_JSON " + json.dumps(rec))
        return
    if args.cmd == "drive":
        tiers = [int(x) for x in args.tiers.split(",")]
        results = drive(tiers, args.reps, args.warmup, args.rss_reps, args.out, args.data_root)
        print(json.dumps(results, indent=2))
        return


if __name__ == "__main__":
    main()
