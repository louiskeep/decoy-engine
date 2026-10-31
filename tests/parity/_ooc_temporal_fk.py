"""Shared builders for the typed out-of-core temporal FK seam tests.

Plan: docs/plans/2026-10-31-slice0-ooc-fk-typed-seam.md (slice 0, item 2). The jobs run end
to end through `run_pipeline` with the real gate, against the full-frame route forced with
`execution_mode="full_frame"` as the reference (P5): the reference column is cast safely to the
out-of-core fixed type before an exact `Array.equals`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution.out_of_core import _batch_join, _join, _stream_join
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s

VERSION = "0.1.0"
GB = 1 << 30
MODES = ("resident", "sink-batch", "sink-reorder")
GATES = ("forced", "large_fk", "byte_estimate")
BASE = dt.datetime(2020, 1, 1)
UNITS = ("s", "ms", "us", "ns")
# The parent relation's masked-key unit after DuckDB's COPY (duckdb 1.5.4 from uv.lock, probed):
# second and millisecond keys are stored as microseconds, nanoseconds stay nanoseconds. The
# relation footer is the authoritative masked type, so it is the FAIL fixed type's unit.
RELATION_UNIT = {"s": "us", "ms": "us", "us": "us", "ns": "ns"}


def ts(unit: str) -> pa.DataType:
    return pa.timestamp(unit)


def instants(n: int, step: dt.timedelta = dt.timedelta(seconds=1)) -> list[dt.datetime]:
    return [BASE + step * i for i in range(n)]


def ns_keys(n: int) -> pa.Array:
    """`n` distinct nanosecond instants 1 ns apart: they collapse to one value at `us`."""
    base = pa.scalar(BASE, ts("ns")).value
    return pa.array([base + i for i in range(n)], ts("ns"))


def job(
    tmp: Path,
    parent_keys: pa.Array,
    child_keys: pa.Array,
    *,
    policy: str = "warn",
    composite: bool = False,
) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    """A pure-mask FK job with passthrough keys (the composite adds an int component)."""
    parent: dict[str, pa.Array] = {"id": parent_keys}
    child: dict[str, pa.Array] = {"parent_id": child_keys}
    if composite:
        parent["n"] = pa.array([i % 3 for i in range(len(parent_keys))], pa.int64())
        child["m"] = pa.array([i % 3 for i in range(len(child_keys))], pa.int64())
    parent["note"] = pa.array(["n"] * len(parent_keys))
    child["tag"] = pa.array(["t"] * len(child_keys))
    p_tbl, c_tbl = pa.table(parent), pa.table(child)
    keys_p, keys_c = (["id", "n"], ["parent_id", "m"]) if composite else (["id"], ["parent_id"])
    tmp.mkdir(parents=True, exist_ok=True)
    pq.write_table(p_tbl, tmp / "parent.parquet")
    pq.write_table(c_tbl, tmp / "child.parquet")
    config = {
        "version": 2,
        "global_settings": {"job_name": "ooc-temporal", "seed": 11},
        "sources": {
            t: {"type": "file", "path": str(tmp / f"{t}.parquet"), "format": "parquet"}
            for t in ("parent", "child")
        },
        "targets": {
            t: {"type": "file", "path": str(tmp / f"{t}.out.parquet"), "format": "parquet"}
            for t in ("parent", "child")
        },
        "tables": [
            {
                "name": "parent",
                "columns": [{"name": c, "strategy": "passthrough"} for c in keys_p]
                + [{"name": "note", "strategy": "redact"}],
            },
            {
                "name": "child",
                "columns": [{"name": c, "strategy": "passthrough"} for c in keys_c]
                + [{"name": "tag", "strategy": "passthrough"}],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": keys_p},
                "children": [{"table": "child", "columns": keys_c}],
                "orphan_policy": policy,
                "namespace": "ns",
            }
        ],
    }
    return config, {"parent": p_tbl, "child": c_tbl}


@dataclass(frozen=True)
class RouteRun:
    outputs: dict[str, pa.Table]
    mode: tuple[str, str]
    warnings: tuple[Any, ...]
    batch_joiners: int
    reorder_joiners: int


def _gate_kwargs(gate: str) -> dict[str, Any]:
    if gate == "forced":
        return {"execution_mode": "out_of_core"}
    if gate == "large_fk":
        return {"use_byte_estimate_routing": False, "out_of_core_threshold_rows": 1}
    if gate == "byte_estimate":
        return {"out_of_core_budget_bytes": GB}
    if gate == "full_frame":
        return {"execution_mode": "full_frame"}
    raise ValueError(gate)


def run(
    config: dict[str, Any],
    sources: dict[str, pa.Table],
    *,
    gate: str,
    mode: str = "resident",
    tmp: Path | None = None,
) -> RouteRun:
    kwargs = _gate_kwargs(gate)
    srcs: dict[str, Any] = dict(sources)
    target: Path | None = None
    if mode != "resident":
        assert tmp is not None, "sink modes need a directory"
        for name in sources:
            srcs[name] = LazySource(Path(config["sources"][name]["path"]))
        target = tmp / f"out-{gate}-{mode}"
        kwargs["sink"] = ParquetTransactionalSink(target)
        if mode == "sink-reorder":
            kwargs["out_of_core_reorder_threshold_rows"] = 0
            kwargs.setdefault("out_of_core_budget_bytes", GB)
    counts = {"batch": 0, "reorder": 0}
    batch_init, reorder_init = ChildFkBatchJoiner.__init__, StreamFkJoiner.__init__

    def spy_batch(self: Any, *a: Any, **k: Any) -> None:
        counts["batch"] += 1
        batch_init(self, *a, **k)

    def spy_reorder(self: Any, *a: Any, **k: Any) -> None:
        counts["reorder"] += 1
        reorder_init(self, *a, **k)

    ChildFkBatchJoiner.__init__ = spy_batch  # type: ignore[method-assign]
    StreamFkJoiner.__init__ = spy_reorder  # type: ignore[method-assign]
    try:
        result = run_pipeline(config, srcs, engine_version=VERSION, **kwargs)
    finally:
        ChildFkBatchJoiner.__init__ = batch_init  # type: ignore[method-assign]
        StreamFkJoiner.__init__ = reorder_init  # type: ignore[method-assign]
    outputs = dict(result.outputs)
    if target is not None:
        outputs = {n: pq.read_table(_published(target, n)) for n in sources}
    execution = result.quality_metrics["execution"]
    return RouteRun(
        outputs,
        (execution["execution_mode"], execution["route_reason"]),
        tuple(result.warnings),
        counts["batch"],
        counts["reorder"],
    )


def _published(target: Path, table: str) -> Path:
    matches = sorted(p for p in target.rglob("*.parquet") if p.stem == table)
    if len(matches) != 1:
        raise AssertionError(f"expected one published file for {table!r}, found {matches}")
    return matches[0]


def orphan_rows(run_: RouteRun) -> int:
    return sum(int(w.detail["orphan_rows"]) for w in run_.warnings if w.code == "orphan_fk")


def assert_matches_reference(reference: pa.Array, got: pa.Array, fixed: pa.DataType) -> None:
    """P5: the full-frame column cast SAFELY to the fixed type equals the out-of-core column
    exactly (values and nulls), and the out-of-core column carries the fixed type."""
    assert got.type == fixed, (got.type, fixed)
    expected = reference.cast(fixed) if reference.type != fixed else reference
    assert got.equals(expected), (got.to_pylist()[:5], expected.to_pylist()[:5])


# --------------------------------------------------------------------------
# Direct-runner shapes (resident out-of-core vs the full-frame adapter), used where a
# composite key needs a composite_fk_group child seed that a passthrough config cannot state.
# --------------------------------------------------------------------------

_K = ["a", "b", "c", "d", "e", "f"]


def scalar_shape(
    policy: OrphanPolicy,
    ptype: pa.DataType,
    ctype: pa.DataType,
    parent: list[Any],
    child: list[Any],
) -> s.Shape:
    """P.n -> C.n, passthrough on both sides."""
    plan = s.make_plan(
        {"P": s.table_seed({"n": s.passthrough()}), "C": s.table_seed({"n": s.passthrough()})}
    )
    return s.Shape(
        plan,
        s.graph(s.edge("P", "n", "C", "n", "ns_n", policy)),
        {
            "P": pa.table({"n": pa.array(parent, ptype)}),
            "C": pa.table({"n": pa.array(child, ctype)}),
        },
    )


def composite_shape(
    policy: OrphanPolicy,
    ptype: pa.DataType,
    ctype: pa.DataType,
    parent: list[Any],
    child: list[Any],
    child_k: list[str | None],
) -> s.Shape:
    """P.(k, n) -> C.(k, n): `k` hashed, `n` passthrough of the caller's types."""
    plan = s.make_plan(
        {
            "P": s.table_seed({"k": s.hashed("ns_pk"), "n": s.passthrough()}),
            "C": s.table_seed({}, {("k", "n"): "ns_cg"}),
        }
    )
    return s.Shape(
        plan,
        s.graph(s.edge("P", ("k", "n"), "C", ("k", "n"), "ns_cg", policy)),
        {
            "P": pa.table({"k": s.strs(_K[: len(parent)]), "n": pa.array(parent, ptype)}),
            "C": pa.table({"k": s.strs(child_k), "n": pa.array(child, ctype)}),
        },
    )


def shape_for(
    composite: bool,
    policy: OrphanPolicy,
    ptype: pa.DataType,
    ctype: pa.DataType,
    parent: list[Any],
    child_refs: list[int | None],
    orphan: Any = None,
) -> s.Shape:
    """`child_refs` index into `parent`; -1 is the orphan value `orphan`; None is a null key."""
    child = [None if r is None else (orphan if r == -1 else parent[r]) for r in child_refs]
    if not composite:
        return scalar_shape(policy, ptype, ctype, parent, child)
    child_k = [None if r is None else ("q" if r == -1 else _K[r]) for r in child_refs]
    return composite_shape(policy, ptype, ctype, parent, child, child_k)


@contextmanager
def counted_orphans() -> Iterator[list[int]]:
    """Sum of the per-batch orphan counts every join surface reports (PRESERVE and REMAP emit
    no warning, and a passthrough orphan publishes the same instant a match would)."""
    total = [0]
    real = _join._append_output_batch

    def spy(*a: Any, **k: Any) -> int:
        n = real(*a, **k)
        total[0] += n
        return n

    with pytest.MonkeyPatch.context() as mp:
        for module in (_batch_join, _stream_join, _join):
            mp.setattr(module, "_append_output_batch", spy)
        yield total


def instants_array(unit: str, n: int) -> pa.Array:
    return pa.array(instants(n), ts(unit))
