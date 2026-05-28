"""Run the engine-v2 re-baseline matrix (S13).

Produces ``tests/perf_fixtures/engine-v2-baseline.json``: for each of the 11
baseline strategies x each fixture tier x BOTH execution substrates (pandas,
polars), a cell carrying wall-time percentiles, RSS delta, the adapter's
boundary-conversion cost, and a per-cell Correctness Gate.

This mirrors the pre-rewrite ``scripts/run_perf_baseline.py`` measurement method
(warmup iteration + iteration clamp for slow cells + psutil RSS), but drives the
V2 ``ExecutionAdapter`` (``PandasExecutionAdapter`` / ``PolarsExecutionAdapter``)
on a ``pa.Table`` instead of the V1 ``StrategyManager``. The flat per-cell shape
matches ``pandas-baseline-pre-rewrite.json`` so ``compare_baselines`` can diff the
two for the Faker >=10x / FPE >=2x gates.

Correctness Gate (per cell): ran-clean on both substrates AND the masked column
actually changed AND the two substrates produce identical ``outputs`` (the S12
parity contract, re-checked live here). A cell that fails the gate has its
performance numbers marked invalid (a faster engine that broke correctness does
not count, per the operating model).

IMPORTANT: ship-quality numbers come from the standardized benchmark CI
(``.github/workflows/benchmark.yml``, pinned ubuntu-latest), NOT a dev box. Local
runs validate that the matrix RUNS and is correctness-green; the wall-time numbers
off an unpinned laptop are indicative only.

Usage::

    python scripts/run_engine_v2_baseline.py --tier small
    python scripts/run_engine_v2_baseline.py --tier small --tier medium
    python scripts/run_engine_v2_baseline.py --strategy hash --tier small
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
import pyarrow as pa

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests"))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from decoy_engine.execution import (  # noqa: E402
    ExecutionResult,
    PandasExecutionAdapter,
    PolarsExecutionAdapter,
)
from decoy_engine.instrumentation import rss_kb  # noqa: E402
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed  # noqa: E402
from decoy_engine.providers_v2 import get_default_registry  # noqa: E402
from decoy_engine.relationships._graph import RelationshipGraph  # noqa: E402
from decoy_engine.relationships._namespace import NamespaceRegistry  # noqa: E402
from perf_fixtures.loaders import available_tiers, load_tier  # noqa: E402
from perf_fixtures.schema import TIERS  # noqa: E402

_BASELINE_JSON = _REPO_ROOT / "tests" / "perf_fixtures" / "engine-v2-baseline.json"
_TABLE = "t"
_JOB_SEED = (0x5713).to_bytes(8, "big")

logging.getLogger("decoy_engine").setLevel(logging.WARNING)
_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())


def _seed(
    strategy: str,
    *,
    provider: str = "x_nobackend",
    namespace: str | None = None,
    deterministic: bool = False,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=provider,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=provider_config,
        coherent_with=(),
    )


def _stringify_date_col(column: str) -> Callable[[pa.Table], pa.Table]:
    """Replace a tz-naive datetime column with its ISO-date string form.

    Deterministic date_shift derives its per-value offset by canonicalizing the
    source value, and the engine hard-errors on tz-naive datetimes (the S5
    canonicalization lock). The documented remediation is to stringify/localize
    upstream; this prep applies exactly that so the cell measures a valid
    deterministic date_shift run (the common string-date source).
    """

    def _prep(table: pa.Table) -> pa.Table:
        idx = table.column_names.index(column)
        iso = [
            (ts.date().isoformat() if ts is not None else None)
            for ts in table.column(column).to_pylist()
        ]
        return table.set_column(idx, column, pa.array(iso, type=pa.string()))

    return _prep


@dataclass(frozen=True)
class V2Cell:
    strategy: str
    column: str
    seed: ColumnSeed
    # Optional per-cell source transform (e.g. stringify a datetime column).
    prep: Callable[[pa.Table], pa.Table] | None = None


# One v2 cell per baseline strategy, mirroring the pre-rewrite strategy<->column
# pairings (tests/perf_fixtures/strategy_rules.py). Deterministic strategies carry
# a namespace so runs are reproducible and the two substrates are comparable.
V2_CELLS: tuple[V2Cell, ...] = (
    V2Cell("passthrough", "customer_id", _seed("passthrough")),
    V2Cell("redact", "ssn", _seed("redact", provider_config=(("redact_with", "XXX"),))),
    V2Cell("truncate", "ssn", _seed("truncate", provider_config=(("length", 4),))),
    V2Cell(
        "faker",
        "full_name",
        _seed(
            "faker",
            provider="person_full_name",
            namespace="bench_faker",
            deterministic=True,
            provider_config=(("pool_size", 256),),
        ),
    ),
    V2Cell(
        "date_shift",
        "dob",
        _seed(
            "date_shift",
            namespace="bench_ds",
            deterministic=True,
            provider_config=(("min_days", -365), ("max_days", 365)),
        ),
        prep=_stringify_date_col("dob"),
    ),
    V2Cell(
        "bucketize",
        "score",
        _seed("bucketize", provider_config=(("width", 100), ("format", "range"))),
    ),
    V2Cell(
        "hash", "email", _seed("hash", provider="hash", namespace="bench_hash", deterministic=True)
    ),
    V2Cell(
        "categorical",
        "status",
        _seed(
            "categorical",
            namespace="bench_cat",
            deterministic=True,
            provider_config=(("categories", ["active", "inactive", "pending", "closed"]),),
        ),
    ),
    V2Cell("shuffle", "email", _seed("shuffle", namespace="bench_shuf", deterministic=True)),
    V2Cell(
        "formula",
        "transaction_amount",
        _seed("formula", provider_config=(("formula", "value * 1.1"),)),
    ),
    V2Cell(
        "fpe",
        "ssn",
        _seed(
            "fpe",
            provider="fpe",
            namespace="bench_fpe",
            deterministic=True,
            provider_config=(("charset", "digits"),),
        ),
    ),
)


def _plan_for(cell: V2Cell) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=((_TABLE, TableSeed(per_column=((cell.column, cell.seed),), per_group=())),),
        )
    )


@dataclass
class SubstrateResult:
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    mean_ms: float = 0.0
    max_ms: float = 0.0
    iterations: int = 0
    boundary_conversion_ms: float = 0.0
    peak_rss_delta_kb: int = 0
    error: str | None = None


@dataclass
class CellResult:
    strategy: str
    tier: str
    column: str
    rows: int
    pandas: dict[str, Any] = field(default_factory=dict)
    polars: dict[str, Any] = field(default_factory=dict)
    correctness_gate: str = "PASS"
    correctness_detail: str = ""


def _p(values: list[float], pct: float) -> float:
    if len(values) < 2:
        return round(max(values), 3) if values else 0.0
    return round(float(statistics.quantiles(values, n=100, method="inclusive")[int(pct) - 1]), 3)


def _run_substrate(
    adapter: Any, cell: V2Cell, table: pa.Table, iterations: int, process: psutil.Process
) -> tuple[SubstrateResult, pa.Table | None]:
    """Run one cell on one substrate; return (metrics, masked output table)."""
    plan = _plan_for(cell)
    sources = {_TABLE: table}
    rss_before = rss_kb()
    process.cpu_percent(interval=None)
    out_table: pa.Table | None = None
    try:
        t0 = time.perf_counter()
        result: ExecutionResult = adapter.run(
            plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        warmup_ms = (time.perf_counter() - t0) * 1000.0
        out_table = result.outputs[_TABLE]
        actual = 1 if warmup_ms > 30_000 else 2 if warmup_ms > 5_000 else iterations
        elapsed: list[float] = []
        boundary_ms: list[float] = []
        for _ in range(actual):
            i0 = time.perf_counter()
            res = adapter.run(
                plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
            )
            elapsed.append((time.perf_counter() - i0) * 1000.0)
            boundary_ms.append(res.boundary_conversion_ms)
    except Exception as exc:
        return SubstrateResult(error=f"{type(exc).__name__}: {exc}"), None
    return (
        SubstrateResult(
            p50_ms=round(statistics.median(elapsed), 3),
            p95_ms=_p(elapsed, 95),
            mean_ms=round(statistics.fmean(elapsed), 3),
            max_ms=round(max(elapsed), 3),
            iterations=len(elapsed),
            boundary_conversion_ms=round(statistics.fmean(boundary_ms), 3),
            peak_rss_delta_kb=max(rss_kb() - rss_before, 0),
        ),
        out_table,
    )


def _correctness(
    cell: V2Cell,
    source: pa.Table,
    pandas_res: SubstrateResult,
    pandas_out: pa.Table | None,
    polars_res: SubstrateResult,
    polars_out: pa.Table | None,
) -> tuple[str, str]:
    """7-gate-aligned per-cell correctness: ran-clean both substrates, the column
    actually masked, and the two substrates agree (S12 parity, value-level)."""
    if pandas_res.error:
        return "FAIL", f"pandas error: {pandas_res.error}"
    if polars_res.error:
        return "FAIL", f"polars error: {polars_res.error}"
    if pandas_out is None or polars_out is None:
        return "FAIL", "missing output"
    col = cell.column
    if pandas_out.column(col).to_pylist() == source.column(col).to_pylist():
        # passthrough is allowed to be identical; everything else must change.
        if cell.strategy != "passthrough":
            return "FAIL", f"{col} unchanged (not masked)"
    if pandas_out.column(col).to_pylist() != polars_out.column(col).to_pylist():
        return "FAIL", "cross-substrate parity mismatch"
    return "PASS", "ran clean; masked; pandas==polars"


def run_matrix(
    tier_names: list[str], strategy_filter: str | None, iterations: int
) -> tuple[list[CellResult], list[str]]:
    process = psutil.Process()
    cells = V2_CELLS
    if strategy_filter:
        cells = tuple(c for c in cells if c.strategy == strategy_filter)
        if not cells:
            raise SystemExit(f"unknown strategy {strategy_filter!r}")
    results: list[CellResult] = []
    skipped_tiers: list[str] = []
    for tier in tier_names:
        if tier not in available_tiers():
            # A REQUESTED tier whose fixture is missing is a loud failure, not a
            # silent skip: a CI run that asked for medium must not quietly produce
            # a small-only baseline (Dennis S13 tooling review).
            print(f"[v2-baseline] MISSING requested tier={tier} (gen via gen_perf_fixtures.py)")
            skipped_tiers.append(tier)
            continue
        table = pa.Table.from_pandas(load_tier(tier), preserve_index=False)
        rows = table.num_rows
        print(f"[v2-baseline] tier={tier} starting ({len(cells)} cells, {rows} rows)")
        for cell in cells:
            cell_table = cell.prep(table) if cell.prep else table
            pandas_res, pandas_out = _run_substrate(
                PandasExecutionAdapter(), cell, cell_table, iterations, process
            )
            polars_res, polars_out = _run_substrate(
                PolarsExecutionAdapter(), cell, cell_table, iterations, process
            )
            gate, detail = _correctness(
                cell, cell_table, pandas_res, pandas_out, polars_res, polars_out
            )
            cr = CellResult(
                strategy=cell.strategy,
                tier=tier,
                column=cell.column,
                rows=rows,
                pandas=_substrate_dict(pandas_res, gate),
                polars=_substrate_dict(polars_res, gate),
                correctness_gate=gate,
                correctness_detail=detail,
            )
            results.append(cr)
            tag = gate if gate == "PASS" else f"{gate} ({detail})"
            print(
                f"  {cell.strategy:<12} pandas p50={pandas_res.p50_ms:.2f}ms "
                f"polars p50={polars_res.p50_ms:.2f}ms  {tag}",
                flush=True,
            )
    return results, skipped_tiers


def _substrate_dict(res: SubstrateResult, gate: str) -> dict[str, Any]:
    d: dict[str, Any] = {
        "p50_ms": res.p50_ms,
        "p95_ms": res.p95_ms,
        "mean_ms": res.mean_ms,
        "max_ms": res.max_ms,
        "iterations": res.iterations,
        "boundary_conversion_ms": res.boundary_conversion_ms,
        "peak_rss_delta_kb": res.peak_rss_delta_kb,
        "error": res.error,
    }
    if res.error or gate != "PASS":
        d["performance_gate"] = "INVALID -- correctness failure"
    return d


def write_results(results: list[CellResult], out_path: Path, env_note: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "schema_version": 1,
            "tool": "scripts/run_engine_v2_baseline.py",
            "substrates": ["pandas", "polars"],
            "environment": env_note,
            "note": (
                "Ship-quality numbers come from the standardized benchmark CI "
                "(.github/workflows/benchmark.yml). Local runs are indicative only."
            ),
        },
        "results": [
            {
                "strategy": r.strategy,
                "tier": r.tier,
                "column": r.column,
                "rows": r.rows,
                "correctness_gate": r.correctness_gate,
                "correctness_detail": r.correctness_detail,
                "pandas": r.pandas,
                "polars": r.polars,
            }
            for r in results
        ],
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[v2-baseline] wrote {len(results)} cells to {out_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run-engine-v2-baseline")
    parser.add_argument("--tier", action="append", choices=[*sorted(TIERS)], default=None)
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--out", type=Path, default=_BASELINE_JSON)
    parser.add_argument(
        "--env-note",
        default="local-dev (indicative; not ship-quality)",
        help="environment label recorded in meta (set to 'ci-ubuntu-latest' on CI)",
    )
    args = parser.parse_args(argv)
    tier_names = args.tier or ["small", "medium"]
    t0 = time.perf_counter()
    results, skipped_tiers = run_matrix(tier_names, args.strategy, args.iterations)
    write_results(results, args.out, args.env_note)
    failed = [r for r in results if r.correctness_gate != "PASS"]
    print(
        f"[v2-baseline] total {time.perf_counter() - t0:.1f}s; "
        f"{len(failed)} correctness failures; {len(skipped_tiers)} missing tier(s)"
    )
    if skipped_tiers:
        print(f"[v2-baseline] MISSING requested tiers: {skipped_tiers} (baseline is incomplete)")
    return 1 if (failed or skipped_tiers) else 0


if __name__ == "__main__":
    sys.exit(main())
