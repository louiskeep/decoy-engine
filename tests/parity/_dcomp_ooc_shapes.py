"""Phase D / Dcomp-OOC: out-of-core runners and component-dtype shapes for the composite cases.

`run_ooc` drives one `Shape` through `run_fk_out_of_core` in each of the three ways the
route is exercised in production: resident (in-memory reassembly, whole-column types), the
sink path on the `_batch_join` lane, and the sink path with the external-reorder driver
forced through the real `out_of_core_reorder_threshold_rows` override. The sink runs read
their output back from the parquet files the sink published, so what is compared is what a
caller would see on disk. A forced-reorder run also counts the joiner classes constructed,
so a route regression to `_batch_join` cannot make a lane comparison vacuous.

The component builders vary ONE key component's Arrow type beside a hashed string component,
which is how a composite tuple carries every dtype the FK typing contract admits; the scalar
controls build the same component as a single-column FK so a strict difference can be
classified as route-level rather than composite-specific.
"""

from __future__ import annotations

import datetime
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
from tests.parity._dcomp_shapes import ALL_TRUE

MODES = ("resident", "sink-batch", "sink-reorder")
SINK_MODES = ("sink-batch", "sink-reorder")
_BUDGET_BYTES = 1 << 30
_DISK_BYTES = 200 << 30

# The one strict dimension every admitted success case differs on: the oracle's pandas
# round trip stamps schema metadata the out-of-core route never adds (D0-G1).
ORACLE_META = [b"pandas"]
OOC_META: list[bytes] = []
METADATA_ONLY = {**ALL_TRUE, "field_metadata": False}

FK_DTYPE_CODE = "out_of_core_fk_key_dtype_unsupported"
ORPHAN_CODE = "orphan_fk_violation"


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
    """Run `shape` out-of-core in `mode`; sink modes need `tmp_path` and read their output back."""
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
            shape.plan,
            sources,
            registry=s.REGISTRY,
            relationship_graph=shape.graph,
            **kwargs,
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
    """Per-table strict report of the out-of-core outputs against the oracle's."""
    assert set(oracle.outputs) == set(run.outputs)
    return {t: sc.compare_strict(oracle.outputs[t], run.outputs[t]) for t in oracle.outputs}


def warning_keys(warnings: Any) -> list[tuple[object, ...]]:
    return [(w.code, w.provider, w.column, w.detail) for w in warnings]


def sorted_warnings(warnings: Any) -> list[tuple[object, ...]]:
    """The warning objects as a multiset: cross-table emission order is route-dependent (G9)."""
    return sorted(warning_keys(warnings), key=repr)


def raised(fn: Any) -> tuple[type[BaseException], str | None, str]:
    """`(type, code, message)` of the exception `fn()` raises; fails if it does not raise."""
    try:
        fn()
    except BaseException as exc:
        return type(exc), getattr(exc, "code", None), str(exc)
    raise AssertionError("expected a raise")


# Component-dtype shapes: P.(k, n) -> C.(k, n), `k` a hashed string, `n` a passthrough key
# component of the caller's Arrow type.


def component_shape(
    policy: OrphanPolicy,
    n_type: pa.DataType,
    *,
    parent_n: list[Any],
    child_k: list[str | None],
    child_n: list[Any],
) -> s.Shape:
    plan = s.make_plan(
        {
            "P": s.table_seed({"k": s.hashed("ns_pk"), "n": s.passthrough()}),
            "C": s.table_seed({}, {("k", "n"): "ns_cg"}),
        }
    )
    g = s.graph(s.edge("P", ("k", "n"), "C", ("k", "n"), "ns_cg", policy))
    parent_k = ["a", "b", "c"][: len(parent_n)]
    return s.Shape(
        plan,
        g,
        {
            "P": pa.table({"k": s.strs(parent_k), "n": pa.array(parent_n, n_type)}),
            "C": pa.table({"k": s.strs(child_k), "n": pa.array(child_n, n_type)}),
        },
    )


def scalar_control_shape(
    policy: OrphanPolicy,
    n_type: pa.DataType,
    *,
    parent_n: list[Any],
    child_n: list[Any],
) -> s.Shape:
    """P.n -> C.n, the single-column FK with the same component values and type."""
    plan = s.make_plan(
        {
            "P": s.table_seed({"n": s.passthrough()}),
            "C": s.table_seed({"n": s.passthrough()}),
        }
    )
    g = s.graph(s.edge("P", "n", "C", "n", "ns_n", policy))
    return s.Shape(
        plan,
        g,
        {
            "P": pa.table({"n": pa.array(parent_n, n_type)}),
            "C": pa.table({"n": pa.array(child_n, n_type)}),
        },
    )


D1, D2, D3 = datetime.date(2020, 1, 1), datetime.date(2021, 1, 1), datetime.date(2022, 1, 1)
T1 = datetime.datetime(2020, 1, 1, 1)
T2 = datetime.datetime(2021, 1, 1)
T3 = datetime.datetime(2022, 1, 1)


def date_component_shape(policy: OrphanPolicy) -> s.Shape:
    """A `date32` component with a matched row, an orphan date and a null string component."""
    return component_shape(
        policy, pa.date32(), parent_n=[D1, D2], child_k=["a", "q", None], child_n=[D1, D3, D1]
    )


def timestamp_component_shape(policy: OrphanPolicy) -> s.Shape:
    return component_shape(
        policy, pa.timestamp("us"), parent_n=[T1, T2], child_k=["a", "q"], child_n=[T1, T3]
    )


def large_string_component_shape(policy: OrphanPolicy) -> s.Shape:
    return component_shape(
        policy, pa.large_string(), parent_n=["x", "y"], child_k=["a", "q"], child_n=["x", "w"]
    )


def bool_component_shape(policy: OrphanPolicy) -> s.Shape:
    return component_shape(
        policy,
        pa.bool_(),
        parent_n=[True, False],
        child_k=["a", "b", "q"],
        child_n=[True, False, True],
    )


def uint64_component_shape(policy: OrphanPolicy, *, values: tuple[int, int, int]) -> s.Shape:
    matched, other, orphan = values
    return component_shape(
        policy,
        pa.uint64(),
        parent_n=[matched, other],
        child_k=["a", "q"],
        child_n=[matched, orphan],
    )


def narrow_int_component_shape(policy: OrphanPolicy, n_type: pa.DataType) -> s.Shape:
    return component_shape(policy, n_type, parent_n=[1, 2], child_k=["a", "q"], child_n=[1, 7])


def nan_parent_component_shape(policy: OrphanPolicy) -> s.Shape:
    """A float NaN in a PARENT key component (that parent row can never be referenced)."""
    return component_shape(
        policy, pa.float64(), parent_n=[1.0, float("nan")], child_k=["a", "q"], child_n=[1.0, 3.0]
    )


def empty_parent_shape(policy: OrphanPolicy) -> s.Shape:
    """A zero-row composite parent; every non-null child tuple is a genuine orphan."""
    base = s.composite_shape(policy)
    empty = pa.table({"k": s.strs([]), "j": s.strs([])})
    child = pa.table({"k": s.strs(["a", "b"]), "j": s.strs(["x", "y"])})
    return s.Shape(base.plan, base.graph, {"P": empty, "C": child})


def hash_evidence(run: OocRun) -> list[dict[str, Any]]:
    return list(run.quality_metrics.get("out_of_core_hash", []))
