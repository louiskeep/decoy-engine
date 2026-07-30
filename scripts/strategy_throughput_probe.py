"""TEST-2 (§TEST Axis A): per-strategy throughput profile.

Answers the plan's Axis-A question -- what is the single-thread rows/sec of
each masking strategy, and how much slower is fpe than hash? -- with an
apples-to-apples run: the SAME row count and a single masked column per
strategy, timed at the pure masking loop (not IO or plan compile).

Distinct from `tests/perf/test_job_performance_gates.py`, which benchmarks
each strategy at a DIFFERENT row/column count as a regression tripwire and so
cannot be compared across strategies. Here every strategy masks one column of
`ROWS` rows, and the reported time is `ExecutionResult.timings[col].elapsed_ms`
(the execution adapter's strategy pass), so the number is the per-value loop
the plan calls the bottleneck (~20k rows/s single-thread), isolated from the
profile/compile/boundary-conversion stack.

Self-contained (synthetic data, no perf_fixtures dependency). Override the
engine location with DECOY_ENGINE_SRC if running from outside the repo.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import tempfile
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

# Allow running the script directly from a checkout without installing.
_SRC = os.environ.get(
    "DECOY_ENGINE_SRC",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from decoy_engine.config import PipelineConfig  # noqa: E402
from decoy_engine.execution import ExecutionResult, run_pipeline  # noqa: E402

ROWS = int(os.environ.get("DECOY_THROUGHPUT_ROWS", "50000"))
_ENGINE_VERSION = "test2-throughput-probe"
# The engine validates source/target as file-backed; the actual data is passed
# in-memory via run_pipeline(sources=...), so these paths are only a valid
# placeholder the config schema accepts (the source file is never read once
# sources= is supplied; the target is written once per run and discarded).
_SRC_PATH = os.path.join(tempfile.gettempdir(), "decoy_throughput_probe_src.parquet")
_OUT_PATH = os.path.join(tempfile.gettempdir(), "decoy_throughput_probe_out.parquet")


def _digits(i: int) -> str:
    return f"{i % 1_000_000_000:09d}"


def _gen_one(source_col: str) -> list[Any]:
    """Build just this strategy's source column (each worker builds its own,
    so no big shared table crosses the process boundary). Shaped so the
    strategy does real work: high-cardinality strings for hash/fpe,
    category-like tokens for categorical, an int for bucketize."""
    gens: dict[str, Any] = {
        "redact_in": lambda: [f"value-{i}" for i in range(ROWS)],
        "hash_in": lambda: [f"id-{i}" for i in range(ROWS)],
        "fpe_in": lambda: [_digits(i) for i in range(ROWS)],
        "categorical_in": lambda: [f"src-{i % 5000}" for i in range(ROWS)],
        "faker_in": lambda: [f"person-{i % 20000}" for i in range(ROWS)],
        "bucketize_in": lambda: list(range(ROWS)),
        "truncate_in": lambda: [f"{10000 + (i % 90000)}" for i in range(ROWS)],
    }
    return gens[source_col]()


# (label, source column, column spec). One masked column per run so the
# timing record isolates exactly that strategy.
_STRATEGIES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("redact", "redact_in", {"strategy": "redact"}),
    ("hash", "hash_in", {"strategy": "hash", "namespace": "hash_ns"}),
    (
        "categorical",
        "categorical_in",
        {
            "strategy": "categorical",
            "provider_config": {
                "categories": [f"cat-{i}" for i in range(50)],
                "weights": [1.0] * 50,
            },
        },
    ),
    (
        "faker",
        "faker_in",
        {
            "strategy": "faker",
            "provider": "person_email",
            "deterministic": True,
            "namespace": "faker_ns",
            "cardinality_mode": "reuse",
            "provider_config": {"pool_size": 2000},
        },
    ),
    (
        "fpe",
        "fpe_in",
        {"strategy": "fpe", "namespace": "fpe_ns", "provider_config": {"charset": "digits"}},
    ),
    ("bucketize", "bucketize_in", {"strategy": "bucketize", "provider_config": {"width": 100}}),
    ("truncate", "truncate_in", {"strategy": "truncate", "provider_config": {"length": 3}}),
)


def _config(source_col: str, spec: dict[str, Any]) -> dict[str, Any]:
    column = {"name": source_col, **spec}
    cfg = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {"t": {"type": "file", "format": "parquet", "path": _SRC_PATH}},
        "tables": [{"name": "t", "columns": [column]}],
        "targets": {"t": {"type": "file", "format": "parquet", "path": _OUT_PATH}},
    }
    return PipelineConfig.model_validate(cfg).model_dump()


def _strategy_ms(result: ExecutionResult) -> float:
    return sum(t.elapsed_ms for t in result.timings)


def _worker(source_col: str, spec: dict[str, Any], q: mp.Queue[float]) -> None:
    """Run ONE strategy in its own process and put the strategy-pass ms on the
    queue. Each strategy gets a fresh interpreter: a single long-lived process
    that calls run_pipeline many times in a loop wedges nondeterministically
    (observed 2026-07-30 -- suspected per-call resource that is not released;
    tracked separately), so the profiler isolates every strategy in its own
    process. One warmup run (primes lazy imports) then one timed run."""
    data = _gen_one(source_col)
    table = pa.table({source_col: pa.array(data)})
    pq.write_table(table, _SRC_PATH)  # placeholder the schema accepts; sources= is authoritative
    cfg = _config(source_col, spec)
    sources = {"t": table}

    def run() -> ExecutionResult:
        return run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False)

    run()  # warmup, uncounted
    q.put(_strategy_ms(run()))


def _profile_one(source_col: str, spec: dict[str, Any], timeout_s: float = 90.0) -> float | None:
    """Spawn a worker for one strategy; return its ms, or None on timeout."""
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    p = ctx.Process(target=_worker, args=(source_col, spec, q))
    p.start()
    p.join(timeout_s)
    if p.is_alive():
        p.terminate()
        p.join()
        return None
    return q.get() if not q.empty() else None


def main() -> None:
    print(f"per-strategy throughput @ {ROWS:,} rows, single masked column, single thread")
    print(f"{'strategy':<14}{'strategy ms':>12}{'rows/sec':>14}", flush=True)
    results: dict[str, float] = {}
    for label, source_col, spec in _STRATEGIES:
        ms = _profile_one(source_col, spec)
        if ms is None:
            print(f"{label:<14}{'TIMEOUT':>12}{'':>14}", flush=True)
            continue
        rows_per_s = ROWS / (ms / 1000.0) if ms > 0 else float("inf")
        results[label] = rows_per_s
        print(f"{label:<14}{ms:>12.1f}{rows_per_s:>14,.0f}", flush=True)

    if "hash" in results and "fpe" in results and results["fpe"] > 0:
        mult = results["hash"] / results["fpe"]
        print(
            f"\nfpe is ~{mult:.1f}x slower than hash "
            f"(hash {results['hash']:,.0f} r/s vs fpe {results['fpe']:,.0f} r/s)"
        )


if __name__ == "__main__":
    main()
