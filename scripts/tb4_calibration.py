#!/usr/bin/env python3
"""TB-4: measure per-route peak-RSS multipliers to REPLACE the unmeasured
placeholder k constants in `_mem_estimate.py` (`K_OUT_OF_CORE_SLOPE`,
`K_SEQUENTIAL_SLOPE`) and re-affirm `K_FULL_FRAME_SLOPE`.

`docs/plans/2026-07-12-track-b-completion-program.md` TB-4, and
`docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` §13 (the placeholder
constants this replaces) + §3.4/B5 (telemetry). Sibling of the TB-3 harness
`scripts/tb3_cgroup_validation.py`; TB-4 does NOT need a kernel cgroup cap --
it needs clean, per-job ATTRIBUTABLE peaks, which `run_pipeline_isolated`
(Sprint 1a subprocess isolation) already gives via VmHWM (`peak_rss_mb`).

Each job runs in a fresh child process so its `peak_rss_mb` is that job's own
high-water mark, not a contaminated process-wide `ru_maxrss` (the exact
requirement §13/B5 pin for recalibration to be trustworthy). For every
(shape, route) we record:

    observed_k = peak_bytes / estimator_basis_bytes

on the SAME basis `estimate_peak_bytes` divides by for that route:
  - full_frame / out_of_core: `raw_data_bytes(all tables).priceable_bytes`
  - sequential: the two-largest-tables working set (fk_cardinality=None), the
    exact pre-k quantity `estimate_peak_bytes(path="sequential")` multiplies.

Shapes span the schema classes §13 says drive k in opposite directions:
  - pooled-string FK: `raw_data_bytes` OVER-prices pooled cells, so observed_k
    reads LOW (the B1 ~1.16 artifact) -- the SAFE-to-over-predict direction.
  - numeric (int64) FK + single-table: no pooling, so observed_k reads HIGH --
    the worst case that a conservative full_frame constant must cover.
  - unique-string single-table: the middle case.

Runtime is bounded and devbox-safe (8 GB box): scales are chosen so no
full_frame peak approaches the box's RAM. Run it directly:

    .venv/bin/python scripts/tb4_calibration.py            # full sweep
    .venv/bin/python scripts/tb4_calibration.py --smoke    # tiny plumbing check
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from decoy_engine.execution._isolated_run import run_pipeline_isolated  # noqa: E402
from decoy_engine.execution._mem_estimate import (  # noqa: E402
    ColumnSizeSpec,
    TableSizeSpec,
    raw_data_bytes,
)

_MB = 1024 * 1024
_HASH_NS = "pns"
Route = Literal["full_frame", "out_of_core", "sequential"]


# --------------------------------------------------------------------------
# Fixture construction: (config, sources, table_specs) triples. `table_specs`
# is the estimator's view of the SAME schema, built with the EXACT declared
# widths, so `raw_data_bytes(table_specs)` is the basis observed_k divides by.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    name: str
    schema_class: str  # "pooled_string" | "numeric" | "unique_string"
    config: dict[str, Any]
    sources: dict[str, pa.Table]
    table_specs: tuple[TableSizeSpec, ...]
    routes: tuple[Route, ...]
    rows: int


def _materialize(work: Path, fx: Fixture) -> dict[str, Any]:
    """Write the fixture's source tables to real Parquet under `work` and
    rewrite the config's source/target paths to those absolute files, so the
    isolated child reads them directly (a genuine on-disk job, the shape a
    real run takes -- run_pipeline loads sources from the config's file
    paths)."""
    import pyarrow.parquet as pq

    work.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(json.dumps(fx.config))
    for name, table in fx.sources.items():
        path = work / f"{name}.parquet"
        pq.write_table(table, path)
        cfg["sources"][name]["path"] = str(path)
        cfg["targets"][name]["path"] = str(work / f"{name}.out.parquet")
    return cfg


def _file_io(work: Path, names: list[str]) -> tuple[dict, dict]:
    sources = {
        n: {"type": "file", "path": str(work / f"{n}.parquet"), "format": "parquet"} for n in names
    }
    targets = {
        n: {"type": "file", "path": str(work / f"{n}.out.parquet"), "format": "parquet"}
        for n in names
    }
    return sources, targets


def _pooled_fk_fixture(rows: int) -> tuple[dict, dict[str, pa.Table], tuple[TableSizeSpec, ...]]:
    """TB-3's pooled-string parent->child FK shape (hash/redact/truncate).
    Payload columns draw from a small shared pool -> `raw_data_bytes` over-
    prices them, so observed_k reads low. 4 payload cols, ~12-byte cells."""
    n_cols, width = 4, 12
    pad = "x" * width
    pool_size = 4096
    parent_cols: dict[str, pa.Array] = {
        "id": pa.array([f"p{i}" for i in range(rows)], type=pa.string())
    }
    child_cols: dict[str, pa.Array] = {
        "cid": pa.array([f"c{i}" for i in range(rows)], type=pa.string()),
        "pid": pa.array([f"p{i % rows}" for i in range(rows)], type=pa.string()),
    }
    for c in range(n_cols):
        parent_cols[f"pn{c}"] = pa.array(
            [f"v{i % pool_size}-{pad}" for i in range(rows)], type=pa.string()
        )
        child_cols[f"cn{c}"] = pa.array(
            [f"w{i % pool_size}-{pad}" for i in range(rows)], type=pa.string()
        )
    sources = {"parent": pa.table(parent_cols), "child": pa.table(child_cols)}

    p_conf: list[dict[str, Any]] = [{"name": "id", "strategy": "hash", "namespace": _HASH_NS}]
    p_conf += [{"name": f"pn{c}", "strategy": "redact"} for c in range(n_cols)]
    c_conf: list[dict[str, Any]] = [
        {"name": "cid", "strategy": "hash", "namespace": "cns"},
        {"name": "pid", "strategy": "hash", "namespace": _HASH_NS},
    ]
    c_conf += [
        {"name": f"cn{c}", "strategy": "truncate", "provider_config": {"length": 4}}
        for c in range(n_cols)
    ]
    config = _fk_config("parent", p_conf, "child", c_conf)

    # Estimator view: key width = avg len of "p<i>"/"c<i>"; payload width 12.
    key_w = _avg_key_len(rows, prefix=1)
    pool_payload_w = float(len(f"v{pool_size - 1}-{pad}"))
    payload = tuple(
        ColumnSizeSpec(name=f"pn{c}", dtype="object", string_width_bytes=pool_payload_w)
        for c in range(n_cols)
    )
    cpayload = tuple(
        ColumnSizeSpec(name=f"cn{c}", dtype="object", string_width_bytes=pool_payload_w)
        for c in range(n_cols)
    )
    specs = (
        TableSizeSpec(
            "parent",
            rows,
            (ColumnSizeSpec("id", "object", string_width_bytes=key_w), *payload),
        ),
        TableSizeSpec(
            "child",
            rows,
            (
                ColumnSizeSpec("cid", "object", string_width_bytes=key_w),
                ColumnSizeSpec("pid", "object", string_width_bytes=key_w),
                *cpayload,
            ),
        ),
    )
    return config, sources, specs


def _numeric_fk_fixture(rows: int) -> tuple[dict, dict[str, pa.Table], tuple[TableSizeSpec, ...]]:
    """Numeric-payload parent->child FK: int64 columns via `passthrough` (an
    out-of-core-eligible strategy, `out_of_core/_compat.py`) + hashed string
    keys. No pooling -> observed_k reads HIGH: the worst case out_of_core /
    sequential constants must cover."""
    n_cols = 12
    parent_cols: dict[str, pa.Array] = {
        "id": pa.array([f"p{i}" for i in range(rows)], type=pa.string())
    }
    child_cols: dict[str, pa.Array] = {
        "cid": pa.array([f"c{i}" for i in range(rows)], type=pa.string()),
        "pid": pa.array([f"p{i % rows}" for i in range(rows)], type=pa.string()),
    }
    for c in range(n_cols):
        parent_cols[f"pv{c}"] = pa.array([i + c for i in range(rows)], type=pa.int64())
        child_cols[f"cv{c}"] = pa.array([i * 2 + c for i in range(rows)], type=pa.int64())
    sources = {"parent": pa.table(parent_cols), "child": pa.table(child_cols)}

    p_conf = [{"name": "id", "strategy": "hash", "namespace": _HASH_NS}]
    p_conf += [{"name": f"pv{c}", "strategy": "passthrough"} for c in range(n_cols)]
    c_conf = [
        {"name": "cid", "strategy": "hash", "namespace": "cns"},
        {"name": "pid", "strategy": "hash", "namespace": _HASH_NS},
    ]
    c_conf += [{"name": f"cv{c}", "strategy": "passthrough"} for c in range(n_cols)]
    config = _fk_config("parent", p_conf, "child", c_conf)

    key_w = _avg_key_len(rows, prefix=1)
    payload = tuple(ColumnSizeSpec(f"pv{c}", "int64") for c in range(n_cols))
    cpayload = tuple(ColumnSizeSpec(f"cv{c}", "int64") for c in range(n_cols))
    specs = (
        TableSizeSpec(
            "parent", rows, (ColumnSizeSpec("id", "object", string_width_bytes=key_w), *payload)
        ),
        TableSizeSpec(
            "child",
            rows,
            (
                ColumnSizeSpec("cid", "object", string_width_bytes=key_w),
                ColumnSizeSpec("pid", "object", string_width_bytes=key_w),
                *cpayload,
            ),
        ),
    )
    return config, sources, specs


def _numeric_single_fixture(
    rows: int,
) -> tuple[dict, dict[str, pa.Table], tuple[TableSizeSpec, ...]]:
    """Single-table 20x int64 (`passthrough`). Matches the numeric shape the
    existing `test_full_frame_estimate_never_undershoots_a_lean_numeric_schema`
    pins -- the full_frame worst case (no pooling)."""
    n_cols = 20
    cols = {f"c{i}": pa.array([r + i for r in range(rows)], type=pa.int64()) for i in range(n_cols)}
    sources = {"t": pa.table(cols)}
    conf = [{"name": f"c{i}", "strategy": "passthrough"} for i in range(n_cols)]
    config = _single_config("t", conf)
    specs = (
        TableSizeSpec("t", rows, tuple(ColumnSizeSpec(f"c{i}", "int64") for i in range(n_cols))),
    )
    return config, sources, specs


def _unique_single_fixture(
    rows: int,
) -> tuple[dict, dict[str, pa.Table], tuple[TableSizeSpec, ...]]:
    """Single-table unique (non-pooled) 16-byte string via `hash`. The middle
    case: observed_k ~1.4 per §13, above pooled's ~1.16, below numeric."""
    vals = pa.array([f"u{i:015d}" for i in range(rows)], type=pa.string())
    sources = {"t": pa.table({"u": vals})}
    conf = [{"name": "u", "strategy": "hash", "namespace": "uns"}]
    config = _single_config("t", conf)
    width = float(len("u" + "0" * 15))
    specs = (TableSizeSpec("t", rows, (ColumnSizeSpec("u", "object", string_width_bytes=width),)),)
    return config, sources, specs


def _avg_key_len(n: int, *, prefix: int) -> float:
    if n <= 0:
        return float(prefix)
    total = 0
    d = 1
    while True:
        start = 0 if d == 1 else 10 ** (d - 1)
        end = 10**d
        if start >= n:
            break
        total += (min(end, n) - start) * d
        d += 1
    return float(prefix) + total / n


def _single_config(name: str, columns: list[dict]) -> dict:
    sources, targets = _file_io(Path("."), [name])
    return {
        "version": 1,
        "global_settings": {"job_name": f"tb4-{name}", "seed": 7},
        "sources": sources,
        "targets": targets,
        "tables": [{"name": name, "columns": columns}],
    }


def _fk_config(p: str, p_cols: list[dict], c: str, c_cols: list[dict]) -> dict:
    sources, targets = _file_io(Path("."), [p, c])
    return {
        "version": 1,
        "global_settings": {"job_name": "tb4-fk", "seed": 7},
        "sources": sources,
        "targets": targets,
        "tables": [{"name": p, "columns": p_cols}, {"name": c, "columns": c_cols}],
        "relationships": [
            {
                "parent": {"table": p, "columns": ["id"]},
                "children": [{"table": c, "columns": ["pid"]}],
                "orphan_policy": "preserve",
                "namespace": _HASH_NS,
            }
        ],
    }


# --------------------------------------------------------------------------
# Basis per route (the pre-k quantity estimate_peak_bytes divides by).
# --------------------------------------------------------------------------


def _route_basis_bytes(specs: tuple[TableSizeSpec, ...], route: Route) -> int:
    if route in ("full_frame", "out_of_core"):
        r = raw_data_bytes(specs)
        assert r.is_priceable, r.unpriceable_columns
        return r.priceable_bytes
    # sequential: two largest tables' raw bytes (fk_cardinality=None path).
    per = [raw_data_bytes((t,)) for t in specs]
    assert all(x.is_priceable for x in per)
    largest_two = sorted((x.priceable_bytes for x in per), reverse=True)[:2]
    return sum(largest_two)


# --------------------------------------------------------------------------
# Measurement.
# --------------------------------------------------------------------------


def _measure(
    fx_config: dict, sources: dict[str, pa.Table], route: Route, budget_bytes: int
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"engine_version": "tb4", "execution_mode": route}
    if route == "out_of_core":
        kwargs["out_of_core_budget_bytes"] = budget_bytes
    result = run_pipeline_isolated(fx_config, sources, **kwargs)
    return {
        "outcome": result.outcome,
        "isolated": result.isolated,
        "peak_rss_mb": result.peak_rss_mb,
        "execution_mode": (
            (result.quality_metrics or {}).get("execution", {}).get("execution_mode")
            if result.quality_metrics
            else None
        ),
        "error": result.error,
    }


# Each shape: builder + which routes to measure. Two row-scales (low, high)
# per shape so a two-point SLOPE k = Delta(peak)/Delta(basis) cancels the
# fixed interpreter/pyarrow intercept (~180 MB on this box) -- the exact
# intercept `_probe.py` fits out and the module docstring names as the reason
# a single-point peak/basis ratio over-reads at small N.
_SHAPES: dict[str, tuple[Any, str, tuple[Route, ...]]] = {
    "pooled_fk": (_pooled_fk_fixture, "pooled_string", ("full_frame", "out_of_core", "sequential")),
    "numeric_fk": (_numeric_fk_fixture, "numeric", ("full_frame", "out_of_core", "sequential")),
    "numeric_single": (_numeric_single_fixture, "numeric", ("full_frame",)),
    "unique_single": (_unique_single_fixture, "unique_string", ("full_frame",)),
}


def _one_point(
    shape: str,
    rows: int,
    routes: tuple[Route, ...],
    schema_class: str,
    budget_bytes: int,
    work_root: Path,
) -> list[dict[str, Any]]:
    """Build ONE (shape, rows) fixture, measure each route isolated, drop it.
    Built one at a time so the parent never holds more than a single fixture's
    resident tables."""
    builder = _SHAPES[shape][0]
    cfg_raw, src, specs = builder(rows)
    fx = Fixture(f"{shape}@{rows}", schema_class, cfg_raw, src, specs, routes, rows)
    cfg = _materialize(work_root / fx.name.replace("@", "_"), fx)
    out: list[dict[str, Any]] = []
    for route in routes:
        basis = _route_basis_bytes(specs, route)
        m = _measure(cfg, src, route, budget_bytes)
        peak_bytes = int(m["peak_rss_mb"] * _MB) if m["peak_rss_mb"] is not None else None
        rec = {
            "fixture": fx.name,
            "shape": shape,
            "schema_class": schema_class,
            "route": route,
            "rows": rows,
            "basis_bytes": basis,
            "basis_mb": round(basis / _MB, 1),
            "peak_bytes": peak_bytes,
            **m,
            "point_k": round(peak_bytes / basis, 4) if peak_bytes and basis else None,
        }
        out.append(rec)
        print(
            f"  {fx.name:20s} {route:12s} basis={rec['basis_mb']:8.1f}MB "
            f"peak={m['peak_rss_mb']}MB {m['outcome']} point_k={rec['point_k']}",
            flush=True,
        )
    return out


def _slope_k(low: dict[str, Any], high: dict[str, Any]) -> float | None:
    """Intercept-free k = (peak_high - peak_low) / (basis_high - basis_low)."""
    if low["peak_bytes"] is None or high["peak_bytes"] is None:
        return None
    d_basis = high["basis_bytes"] - low["basis_bytes"]
    if d_basis <= 0:
        return None
    return round((high["peak_bytes"] - low["peak_bytes"]) / d_basis, 4)


def run(scale: dict[str, list[int]], budget_bytes: int, work_root: Path) -> dict[str, Any]:
    measurements: list[dict[str, Any]] = []
    # (shape, route) -> {rows -> record}, to pair low/high for the slope.
    for shape, sizes in scale.items():
        _, schema_class, routes = _SHAPES[shape]
        for rows in sizes:
            measurements.extend(
                _one_point(shape, rows, routes, schema_class, budget_bytes, work_root)
            )
    # Two-point slope per (shape, route) using each shape's two scales.
    slopes: list[dict[str, Any]] = []
    for shape, sizes in scale.items():
        if len(sizes) < 2:
            continue
        lo_rows, hi_rows = min(sizes), max(sizes)
        _, _, routes = _SHAPES[shape]
        for route in routes:
            lo = next(
                (
                    m
                    for m in measurements
                    if m["shape"] == shape and m["route"] == route and m["rows"] == lo_rows
                ),
                None,
            )
            hi = next(
                (
                    m
                    for m in measurements
                    if m["shape"] == shape and m["route"] == route and m["rows"] == hi_rows
                ),
                None,
            )
            if (
                lo is None
                or hi is None
                or lo["outcome"] != "completed"
                or hi["outcome"] != "completed"
            ):
                continue
            k = _slope_k(lo, hi)
            if k is not None:
                slopes.append(
                    {
                        "shape": shape,
                        "schema_class": _SHAPES[shape][1],
                        "route": route,
                        "rows_low": lo_rows,
                        "rows_high": hi_rows,
                        "point_k_low": lo["point_k"],
                        "point_k_high": hi["point_k"],
                        "slope_k": k,
                    }
                )
                print(f"  SLOPE {shape:16s} {route:12s} slope_k={k}", flush=True)
    return {"budget_bytes": budget_bytes, "measurements": measurements, "slope_k": slopes}


def _aggregate(report: dict[str, Any]) -> dict[str, Any]:
    by_route: dict[str, list[float]] = {}
    for s in report["slope_k"]:
        by_route.setdefault(s["route"], []).append(s["slope_k"])
    report["aggregate_slope_k"] = {
        r: {"max_k": max(ks), "min_k": min(ks), "n": len(ks)} for r, ks in by_route.items()
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="TB-4 k-constant calibration measurement")
    ap.add_argument("--smoke", action="store_true", help="tiny plumbing check")
    ap.add_argument("--budget-mb", type=int, default=2048)
    ap.add_argument("--out", type=str, default=None)
    ap.add_argument("--work-dir", type=str, default="/tmp/claude-1000/-home-cam/scratch/tb4")
    args = ap.parse_args()
    if args.smoke:
        scale = {
            "pooled_fk": [5_000, 10_000],
            "numeric_fk": [5_000, 10_000],
            "numeric_single": [10_000, 20_000],
            "unique_single": [10_000, 20_000],
        }
    else:
        scale = {
            "pooled_fk": [400_000, 800_000],
            "numeric_fk": [500_000, 1_000_000],
            "numeric_single": [1_000_000, 2_000_000],
            "unique_single": [1_000_000, 2_000_000],
        }
    print(f"TB-4 calibration sweep (smoke={args.smoke}, budget={args.budget_mb}MB)", flush=True)
    report = _aggregate(run(scale, args.budget_mb * _MB, Path(args.work_dir)))
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print("\n=== two-point slope_k by route (intercept-free) ===")
    for r, a in report["aggregate_slope_k"].items():
        print(f"  {r:12s} max_k={a['max_k']:.4f} min_k={a['min_k']:.4f} n={a['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
