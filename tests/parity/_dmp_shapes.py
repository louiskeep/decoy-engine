"""Phase D / Dmp: multi-parent FK shapes and the out-of-core runner for their acceptance cases.

A multi-parent child declares one column tuple against N parents. Every builder here gives
each parent its OWN hash namespace, so a key present in two parents has two different tokens
and the first-hit-wins precedence is observable in the output (a reversed precedence cannot
hide behind equal tokens). `run_ooc` drives a `Shape` through `run_fk_out_of_core` resident,
on the sink path, and on the sink path with the external-reorder driver forced, reading sink
output back from the published parquet; it mirrors the Dcomp-OOC runner so the two slices'
contracts read the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _strict_compare as sc

MODES = ("resident", "sink-batch", "sink-reorder")
ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)
# The one strict dimension every admitted success case differs on (D0-G1).
METADATA_ONLY = {**ALL_TRUE, "field_metadata": False}
FK_DTYPE_CODE = "out_of_core_fk_key_dtype_unsupported"
ORPHAN_CODE = "orphan_fk_violation"
_BUDGET_BYTES = 1 << 30
_DISK_BYTES = 200 << 30


@dataclass(frozen=True)
class OocRun:
    outputs: dict[str, pa.Table]
    warnings: tuple[Any, ...]
    quality_metrics: dict[str, Any]
    batch_joiners: int
    reorder_joiners: int


def run_ooc(
    shape: s.Shape,
    mode: str = "resident",
    *,
    tmp_path: Path | None = None,
    batch_rows: int | None = None,
    native_threads: int = 1,
) -> OocRun:
    if mode not in MODES:
        raise ValueError(mode)
    kwargs: dict[str, Any] = {"batch_rows": batch_rows, "native_threads": native_threads}
    sources: dict[str, Any] = dict(shape.sources)
    target: Path | None = None
    if mode != "resident":
        if tmp_path is None:
            raise ValueError("sink modes need tmp_path")
        tmp_path.mkdir(parents=True, exist_ok=True)
        for name, table in shape.sources.items():
            path = tmp_path / f"src_{name}.parquet"
            pq.write_table(table, path)
            sources[name] = LazySource(path)
        target = tmp_path / "out"
        kwargs["sink"] = ParquetTransactionalSink(target)
        kwargs["temp_dir"] = tmp_path / "work"
        if mode == "sink-reorder":
            kwargs.update(
                budget_bytes=_BUDGET_BYTES,
                temp_disk_budget_bytes=_DISK_BYTES,
                out_of_core_reorder_threshold_rows=0,
            )
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
        result = run_fk_out_of_core(
            shape.plan, sources, registry=s.REGISTRY, relationship_graph=shape.graph, **kwargs
        )
    finally:
        ChildFkBatchJoiner.__init__ = batch_init  # type: ignore[method-assign]
        StreamFkJoiner.__init__ = reorder_init  # type: ignore[method-assign]
    outputs = dict(result.outputs)
    if target is not None:
        outputs = {name: pq.read_table(_published(target, name)) for name in shape.sources}
    return OocRun(
        outputs, result.warnings, dict(result.quality_metrics), counts["batch"], counts["reorder"]
    )


def _published(target: Path, table: str) -> Path:
    matches = sorted(p for p in target.rglob("*.parquet") if p.stem == table)
    if len(matches) != 1:
        raise AssertionError(f"expected one published file for {table!r}, found {matches}")
    return matches[0]


def strict(oracle: Any, run: OocRun) -> dict[str, dict[str, bool]]:
    assert set(oracle.outputs) == set(run.outputs)
    return {t: sc.compare_strict(oracle.outputs[t], run.outputs[t]) for t in oracle.outputs}


def warning_keys(warnings: Any) -> list[tuple[object, ...]]:
    return [(w.code, w.provider, w.column, w.detail) for w in warnings]


def sorted_warnings(warnings: Any) -> list[tuple[object, ...]]:
    """As a multiset: cross-table warning order is route-dependent (Dcomp-OOC G9)."""
    return sorted(warning_keys(warnings), key=repr)


def raised(fn: Any) -> tuple[type[Exception], str | None, str]:
    """`(type, code, message)` of the exception `fn()` raises; fails if it does not raise."""
    try:
        fn()
    except Exception as exc:
        return type(exc), getattr(exc, "code", None), str(exc)
    raise AssertionError("expected a raise")


def hash_evidence(run: OocRun) -> list[dict[str, Any]]:
    return list(run.quality_metrics.get("out_of_core_hash", []))


# Shapes. Parent `i` masks under namespace `ns_p{i}`; the child under `ns_x`.


def three_parent_shape(policy: OrphanPolicy) -> s.Shape:
    """P1.k, P2.k, P3.k -> X.ref. Every rank wins at least once; `all3` is in every parent,
    `p23` in the last two, and `nowhere` in none."""
    plan = s.make_plan(
        {
            "P1": s.table_seed({"k": s.hashed("ns_p1")}),
            "P2": s.table_seed({"k": s.hashed("ns_p2")}),
            "P3": s.table_seed({"k": s.hashed("ns_p3")}),
            "X": s.table_seed({"ref": s.hashed("ns_x")}),
        }
    )
    g = s.graph(
        s.edge("P1", "k", "X", "ref", "ns_x", policy),
        s.edge("P2", "k", "X", "ref", "ns_x", policy),
        s.edge("P3", "k", "X", "ref", "ns_x", policy),
    )
    return s.Shape(
        plan,
        g,
        {
            "P1": pa.table({"k": s.strs(["only1", "all3"])}),
            "P2": pa.table({"k": s.strs(["only2", "all3", "p23"])}),
            "P3": pa.table({"k": s.strs(["only3", "all3", "p23"])}),
            "X": pa.table(
                {"ref": s.strs(["only1", "only2", "only3", "all3", "p23", None, "nowhere"])}
            ),
        },
    )


def reversed_shape(shape: s.Shape) -> s.Shape:
    """The same job with the edges declared in the opposite order."""
    return s.Shape(shape.plan, s.graph(*reversed(shape.graph.edges)), shape.sources)


def composite_multi_parent_shape(policy: OrphanPolicy) -> s.Shape:
    """P1.(k, j) and P2.(k, j) -> C.(k, j): `s` is a shared tuple, `z` an orphan, one row null."""
    plan = s.make_plan(
        {
            "P1": s.table_seed({"k": s.hashed("ns_p1k"), "j": s.hashed("ns_p1j")}),
            "P2": s.table_seed({"k": s.hashed("ns_p2k"), "j": s.hashed("ns_p2j")}),
            "C": s.table_seed({}, {("k", "j"): "ns_cg"}),
        }
    )
    g = s.graph(
        s.edge("P1", ("k", "j"), "C", ("k", "j"), "ns_cg", policy),
        s.edge("P2", ("k", "j"), "C", ("k", "j"), "ns_cg", policy),
    )
    return s.Shape(
        plan,
        g,
        {
            "P1": pa.table({"k": s.strs(["a", "s"]), "j": s.strs(["x", "t"])}),
            "P2": pa.table({"k": s.strs(["b", "s"]), "j": s.strs(["y", "t"])}),
            "C": pa.table(
                {"k": s.strs(["a", "b", "s", "z", None]), "j": s.strs(["x", "y", "t", "z", "x"])}
            ),
        },
    )


def int_passthrough_shape(policy: OrphanPolicy, *, p2_type: pa.DataType | None = None) -> s.Shape:
    """P1.k (int64 passthrough) and P2.k (`p2_type` passthrough) -> X.ref (int64 passthrough).

    With `p2_type` unset both parents mask to int64 (admitted); with `float64` the two masked
    key types differ, the shape the merge refuses."""
    p2_type = p2_type or pa.int64()
    plan = s.make_plan(
        {
            "P1": s.table_seed({"k": s.passthrough()}),
            "P2": s.table_seed({"k": s.passthrough()}),
            "X": s.table_seed({"ref": s.passthrough()}),
        }
    )
    g = s.graph(
        s.edge("P1", "k", "X", "ref", "ns_x", policy), s.edge("P2", "k", "X", "ref", "ns_x", policy)
    )
    return s.Shape(
        plan,
        g,
        {
            "P1": pa.table({"k": pa.array([1, 2], pa.int64())}),
            "P2": pa.table({"k": pa.array([2, 3], p2_type)}),
            "X": pa.table({"ref": pa.array([1, 2, 3, 9, None], pa.int64())}),
        },
    )


def wide_shape(policy: OrphanPolicy, *, rows: int) -> s.Shape:
    """P1 holds the even keys and P2 every key; the child cycles through all of them plus an
    orphan every seventh row, so overlapping keys fall on every batch boundary."""
    keys = [f"k{i}" for i in range(rows)]
    child = [f"orphan{i}" if i % 7 == 0 else keys[i % rows] for i in range(rows * 2)]
    plan = s.make_plan(
        {
            "P1": s.table_seed({"k": s.hashed("ns_p1")}),
            "P2": s.table_seed({"k": s.hashed("ns_p2")}),
            "X": s.table_seed({"ref": s.hashed("ns_x")}),
        }
    )
    g = s.graph(
        s.edge("P1", "k", "X", "ref", "ns_x", policy), s.edge("P2", "k", "X", "ref", "ns_x", policy)
    )
    return s.Shape(
        plan,
        g,
        {
            "P1": pa.table({"k": s.strs(keys[::2])}),
            "P2": pa.table({"k": s.strs(keys)}),
            "X": pa.table({"ref": s.strs(child)}),
        },
    )
