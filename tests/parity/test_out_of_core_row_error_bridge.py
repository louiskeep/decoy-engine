"""F2 row-error channel: bucketize and top_code on the out-of-core route.

These are row-local in VALUE but emit per-value `format_error` RowErrors. They
run on the out-of-core route through the per-batch oracle-handler bridge
(`out_of_core/_row_error_bridge.py`), so their masked output must be
byte-identical to the full-frame pandas oracle, and an uncoercible cell must
fail the job CLOSED (the chunked route's H1 contract; neither non-full-frame
lane quarantines row errors), with the reported row index ABSOLUTE in the whole
source, never batch-local.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import PandasExecutionAdapter, ParquetTransactionalSink
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_MIB = 1024 * 1024
_REORDER_BUDGET = 1024 * _MIB  # generous: a tiny fixture stays below the key ceiling
_DISK = 200 * 1024**3

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_JOB_SEED = b"\x11" * 8


def _seed(strategy: str, *, provider_config: tuple[tuple[str, Any], ...] = ()) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy=strategy,
        provider=strategy,
        backend_type="decoy_native",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=provider_config,
        coherent_with=(),
    )


def _key_seed() -> ColumnSeed:
    return ColumnSeed(
        namespace="kns",
        strategy="hash",
        provider="hash",
        backend_type="decoy_native",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _job(
    payload_seed: ColumnSeed, parent_vals: list[str | None], child_vals: list[str | None]
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    key = _key_seed()
    parent = pa.table(
        {
            "pk": pa.array([f"p{i}" for i in range(len(parent_vals))], type=pa.string()),
            "pay": pa.array(parent_vals, type=pa.string()),
        }
    )
    child = pa.table(
        {
            "fk": pa.array([f"p{i}" for i in range(len(child_vals))], type=pa.string()),
            "cpay": pa.array(child_vals, type=pa.string()),
        }
    )
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(
                ("parent", TableSeed(per_column=(("pk", key), ("pay", payload_seed)), per_group=())),
                ("child", TableSeed(per_column=(("fk", key), ("cpay", payload_seed)), per_group=())),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="kns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "child": child}, graph


def _gate_admits(plan: Any, graph: RelationshipGraph) -> bool:
    work = order_work(build_work_list(plan, _REG), graph)
    return check_out_of_core_compatibility(plan, work, graph).accepted


def _assert_value_equal(oracle: pa.Table, ooc: pa.Table, label: str) -> None:
    got, want = ooc.to_pydict(), oracle.to_pydict()
    assert set(got) == set(want), f"{label}: columns {set(got)} vs {set(want)}"
    for name in want:
        assert got[name] == want[name], (
            f"{label}: {name!r} diverges\n oracle={want[name]}\n    ooc={got[name]}"
        )


_CLEAN = {
    "bucketize": (
        _seed("bucketize", provider_config=(("width", 10),)),
        ["25", "95", "40", None, "10", "72"],
    ),
    "top_code": (
        _seed("top_code", provider_config=(("cap", 89), ("over_label", "90+"))),
        ["25", "95", "40", None, "10", "72"],
    ),
    "top_code_preset": (
        _seed("top_code", provider_config=(("preset", "hipaa_age"),)),
        ["3", "90", "89", "91", None, "40"],
    ),
}


@pytest.mark.parametrize("kind", list(_CLEAN))
@pytest.mark.parametrize("batch_rows", [None, 3, 2, 1])
def test_row_error_bridge_payload_parity(kind: str, batch_rows: int | None) -> None:
    """A clean (fully coercible) bucketize/top_code payload on parent + child is
    byte-identical to the oracle, at every batch size (a batch boundary must not
    change a canonical rendering -- the CHUNK_SAFE property these handlers have)."""
    payload_seed, vals = _CLEAN[kind]
    plan, sources, graph = _job(payload_seed, vals, list(reversed(vals)))
    assert _gate_admits(plan, graph), f"{kind}: expected gate to admit"
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    ooc = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, batch_rows=batch_rows
    )
    for table in oracle.outputs:
        _assert_value_equal(oracle.outputs[table], ooc.outputs[table], f"{kind}/batch={batch_rows}")


@pytest.mark.parametrize("strategy", ["bucketize", "top_code"])
def test_row_error_fails_closed(strategy: str) -> None:
    """An uncoercible cell fails the job closed with RowErrorsFailedError rather
    than silently keeping the raw value, mirroring the chunked route."""
    seed = (
        _seed("bucketize", provider_config=(("width", 10),))
        if strategy == "bucketize"
        else _seed("top_code", provider_config=(("cap", 89), ("over_label", "90+")))
    )
    plan, sources, graph = _job(seed, ["25", "nope", "40"], ["25", "40", "10"])
    assert _gate_admits(plan, graph)
    with pytest.raises(RowErrorsFailedError) as exc:
        run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    triggers = {r.trigger for r in exc.value.records}
    assert triggers == {"format_error"}, triggers
    assert {r.column for r in exc.value.records} == {"pay"}


@pytest.mark.parametrize("batch_rows", [None, 2, 1])
def test_row_error_index_is_absolute(batch_rows: int | None) -> None:
    """The reported row index is the uncoercible cell's position in the WHOLE
    source, not batch-local: the bad cell at parent row 4 must report row_index 4
    even when it lands in a later batch (the durable Phase-1 row offset)."""
    seed = _seed("bucketize", provider_config=(("width", 10),))
    parent_vals = ["10", "20", "30", "40", "bad", "60"]  # uncoercible at index 4
    plan, sources, graph = _job(seed, parent_vals, ["10", "20", "30", "40", "50", "60"])
    with pytest.raises(RowErrorsFailedError) as exc:
        run_fk_out_of_core(
            plan, sources, registry=_REG, relationship_graph=graph, batch_rows=batch_rows
        )
    pay_errors = [r for r in exc.value.records if r.column == "pay"]
    assert [r.row_index for r in pay_errors] == [4], [r.row_index for r in pay_errors]
    assert pay_errors[0].table == "parent"


# ---------------------------------------------------------------------------
# Reorder driver (sink path): the child's payload masks through the reorder
# driver's own Phase-1 bridge, the parent's through the batch driver.
# ---------------------------------------------------------------------------


def _run_sink(plan: Any, sources: Any, graph: Any, target: Path) -> dict[str, bool]:
    saw = {"batch": False, "reorder": False}
    orig_batch, orig_reorder = ChildFkBatchJoiner.__init__, StreamFkJoiner.__init__

    def spy_batch(self: Any, *a: Any, **k: Any) -> None:
        saw["batch"] = True
        orig_batch(self, *a, **k)

    def spy_reorder(self: Any, *a: Any, **k: Any) -> None:
        saw["reorder"] = True
        orig_reorder(self, *a, **k)

    ChildFkBatchJoiner.__init__ = spy_batch  # type: ignore[method-assign]
    StreamFkJoiner.__init__ = spy_reorder  # type: ignore[method-assign]
    try:
        run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            sink=ParquetTransactionalSink(target),
            temp_dir=target.parent / f"{target.name}-work",
            budget_bytes=_REORDER_BUDGET,
            temp_disk_budget_bytes=_DISK,
            out_of_core_reorder_threshold_rows=0,
        )
    finally:
        ChildFkBatchJoiner.__init__ = orig_batch  # type: ignore[method-assign]
        StreamFkJoiner.__init__ = orig_reorder  # type: ignore[method-assign]
    return saw


@pytest.mark.parametrize("kind", ["bucketize", "top_code"])
def test_row_error_bridge_reorder_driver_parity(kind: str, tmp_path: Path) -> None:
    """The child payload masks through the REORDER driver's Phase-1 bridge
    (threshold_rows=0 forces it); its sink output stays byte-identical to the
    oracle, and the parent's payload (batch driver) does too."""
    payload_seed, vals = _CLEAN[kind]
    plan, sources, graph = _job(payload_seed, vals, list(reversed(vals)))
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    out = tmp_path / "out"
    saw = _run_sink(plan, sources, graph, out)
    assert saw["reorder"], "threshold_rows=0 should route the child to the reorder driver"
    for name in ("parent", "child"):
        written = pq.read_table(out / f"{name}.parquet")
        _assert_value_equal(oracle.outputs[name], written, f"reorder/{kind}:{name}")


def test_row_error_bridge_reorder_driver_fails_closed(tmp_path: Path) -> None:
    """A bad cell in the CHILD payload fails closed on the reorder driver too."""
    seed = _seed("bucketize", provider_config=(("width", 10),))
    plan, sources, graph = _job(seed, ["10", "20", "30"], ["10", "nope", "30"])
    with pytest.raises(RowErrorsFailedError) as exc:
        _run_sink(plan, sources, graph, tmp_path / "out")
    assert {r.trigger for r in exc.value.records} == {"format_error"}
    assert {r.column for r in exc.value.records} == {"cpay"}
