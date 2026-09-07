"""Wall-time crossover calibration: reorder route vs `_batch_join`, by
parent-key count (docs/plans/2026-09-03-p4-task7-route-seam.md section 7).

Drives the REAL route seam (`run_fk_out_of_core`, via the live
`out_of_core_reorder_threshold_rows` kwarg) rather than the low-level join
primitives directly, so the measured numbers reflect what a production job
actually experiences -- including masking, staging, and the sink write, not
just the join itself.

For each shape (parent-key count x masked payload width), both routes run
`R` repetitions (warmup reps discarded), reporting median and p90 wall time
(linear-interpolation quantiles). Pass rule for the `REORDER_PARENT_KEY_
THRESHOLD` default (2,000,000):

  - at parent_key_count >= 2,000,000: reorder's median <= _batch_join's
    median by a margin >= 20%, AND reorder's p90 <= _batch_join's median
    (the tail does not erase the win).
  - below ~1,000,000: no regression claim is made -- the two routes are
    expected to sit within run-to-run variance of each other.

Run: `.venv/bin/python scripts/native-testing/reorder_crossover_bench.py
[--quick]`. `--quick` runs a small, fast sample (documented below) rather
than the full sweep -- see the recorded results file this script writes
alongside itself for exactly which sample backs the currently-checked-in
numbers, and note the honest limitation: a full-scale (2M+ parent-key,
multi-fan-in) sweep was not run in this environment; the sample below
validates the SHAPE of the crossover (reorder pulls ahead as parent-key
count grows) at a scale this devbox can complete quickly, not the exact
20%-margin claim at the true 2M default -- that full-scale run is deferred,
per plan section 7's "live per-host calibration deferred (Q3)" scope.
"""

from __future__ import annotations

import json
import platform
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

_REG = get_default_registry()
_JOB_SEED = b"\x99" * 8
_RESULTS_PATH = Path(__file__).resolve().parent / "reorder_crossover_bench_results.json"

_BUDGET_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB, generous -- timing run, not a memory proof
_DISK_BYTES = 200 * 1024 * 1024 * 1024
_CHILD_ROWS = 200_000
_WARMUP_REPS = 1
_REPS = 5


def _seed(namespace: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _plan(payload_width: int) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(
                ("parent", TableSeed(per_column=(("pk", _seed("ns")),), per_group=())),
                ("child", TableSeed(per_column=(("fk", _seed("ns")),), per_group=())),
            ),
        )
    )


def _graph() -> RelationshipGraph:
    return RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="ns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )


def _sources(parent_rows: int, child_rows: int, payload_width: int) -> dict[str, pa.Table]:
    parent_keys = [f"p{i:012d}" for i in range(parent_rows)]
    parent = pa.table(
        {
            "pk": pa.array(parent_keys, type=pa.string()),
            "payload": pa.array(["x" * payload_width] * parent_rows, type=pa.string()),
        }
    )
    child_fk = [parent_keys[i % parent_rows] for i in range(child_rows)]
    child = pa.table(
        {
            "fk": pa.array(child_fk, type=pa.string()),
            "payload": pa.array(["y" * payload_width] * child_rows, type=pa.string()),
        }
    )
    return {"parent": parent, "child": child}


def _timed_run(
    plan: Any, sources: dict[str, pa.Table], graph: RelationshipGraph, *, force_reorder: bool
) -> float:
    work_root = Path(tempfile.mkdtemp(prefix="reorder-crossover-"))
    try:
        kwargs: dict[str, Any] = {}
        if force_reorder:
            kwargs.update(
                budget_bytes=_BUDGET_BYTES,
                temp_disk_budget_bytes=_DISK_BYTES,
                out_of_core_reorder_threshold_rows=0,
            )
        sink = ParquetTransactionalSink(work_root / "out")
        start = time.monotonic()
        run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            sink=sink,
            temp_dir=work_root / "work",
            **kwargs,
        )
        return time.monotonic() - start
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def _quantile(values: list[float], q: float) -> float:
    """Linear-interpolation quantile (the method this benchmark's pass rule
    is stated in terms of), matching `statistics.quantiles`'s default `n=100,
    method="linear"` inclusive convention rather than re-deriving one."""
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    percentiles = statistics.quantiles(ordered, n=100, method="inclusive")
    idx = max(0, min(99, round(q * 100) - 1))
    return percentiles[idx]


def _measure_shape(
    parent_rows: int, payload_width: int, *, reps: int, warmup: int
) -> dict[str, Any]:
    plan = _plan(payload_width)
    graph = _graph()
    sources = _sources(parent_rows, _CHILD_ROWS, payload_width)

    def _run_route(force_reorder: bool) -> list[float]:
        for _ in range(warmup):
            _timed_run(plan, sources, graph, force_reorder=force_reorder)
        return [_timed_run(plan, sources, graph, force_reorder=force_reorder) for _ in range(reps)]

    batch_times = _run_route(False)
    reorder_times = _run_route(True)
    batch_median = statistics.median(batch_times)
    reorder_median = statistics.median(reorder_times)
    reorder_p90 = _quantile(reorder_times, 0.90)
    margin = (batch_median - reorder_median) / batch_median if batch_median else 0.0
    return {
        "parent_rows": parent_rows,
        "child_rows": _CHILD_ROWS,
        "payload_width": payload_width,
        "batch_join_times_s": batch_times,
        "reorder_times_s": reorder_times,
        "batch_join_median_s": batch_median,
        "reorder_median_s": reorder_median,
        "reorder_p90_s": reorder_p90,
        "reorder_margin_vs_batch_median": margin,
        "reorder_p90_le_batch_median": reorder_p90 <= batch_median,
    }


def main() -> None:
    quick = "--quick" in sys.argv
    # The full 2M-default sweep (parent_rows up to 2,000,000+, multiple fan-in
    # values) is deferred (plan section 7, "live per-host calibration
    # deferred (Q3)") -- this devbox run is a SAMPLE proving the crossover's
    # SHAPE at a scale that completes in well under a minute. `--quick` is
    # the only mode implemented; a future full run would extend
    # `parent_rows_sweep` and `payload_widths` and raise `_REPS`.
    parent_rows_sweep = (5_000, 200_000) if quick else (5_000, 200_000, 2_000_000)
    payload_widths = (8, 64)

    shapes = []
    for parent_rows in parent_rows_sweep:
        for width in payload_widths:
            print(f"measuring parent_rows={parent_rows} payload_width={width}...", file=sys.stderr)
            shapes.append(_measure_shape(parent_rows, width, reps=_REPS, warmup=_WARMUP_REPS))

    record = {
        "warmup_reps_discarded": _WARMUP_REPS,
        "repetitions": _REPS,
        "child_rows": _CHILD_ROWS,
        "parent_rows_sweep": list(parent_rows_sweep),
        "payload_widths": list(payload_widths),
        "quantile_method": "linear interpolation (statistics.quantiles, method='inclusive')",
        "environment": {
            "cpu_count": __import__("os").cpu_count(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "disk_type": "nvme-ssd (devbox .55)",
        },
        "dependency_versions": {
            "pyarrow": pa.__version__,
            "duckdb": __import__("duckdb").__version__,
        },
        "sample_scope": "quick" if quick else "full",
        "shapes": shapes,
    }
    _RESULTS_PATH.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"wrote {_RESULTS_PATH}", file=sys.stderr)

    for shape in shapes:
        tag = f"parent_rows={shape['parent_rows']} width={shape['payload_width']}"
        print(
            f"{tag}: batch_join median={shape['batch_join_median_s']:.4f}s "
            f"reorder median={shape['reorder_median_s']:.4f}s "
            f"reorder p90={shape['reorder_p90_s']:.4f}s "
            f"margin={shape['reorder_margin_vs_batch_median']:+.1%}"
        )


if __name__ == "__main__":
    main()
