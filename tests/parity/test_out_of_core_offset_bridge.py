"""F3 row-offset channel: windowed_date on the out-of-core route.

windowed_date is position-keyed: each row's output date keys on the row's GLOBAL
position in the whole, unchunked source. The out-of-core runner threads that
durable ordinal into mask_batch, so windowed_date runs through the same
full-frame handler per batch (`out_of_core/_offset_bridge.py`), byte-identical
to the oracle and INVARIANT to the batch size. It is admitted only when its
anchor column is unmasked or passthrough, so the raw anchor the bridge reads
matches the oracle's reading; a masked anchor declines to full-frame.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import PandasExecutionAdapter, ParquetTransactionalSink
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_JOB_SEED = b"\x11" * 8
_MIB = 1024 * 1024


def _seed(strategy: str, *, namespace: str | None = None, pc: tuple[tuple[str, Any], ...] = ()):
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="decoy_native",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=pc,
        coherent_with=(),
    )


_KEY = _seed("hash", namespace="kns")
_PASSTHROUGH = _seed("passthrough")
_ANCHORS = ["2020-01-01", "2019-06-15", None, "2021-12-31", "2000-02-29", "2022-07-04"]


def _job(anchor_seed: ColumnSeed) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    wd = _seed("windowed_date", pc=(("anchor", "dob"), ("max_days", 30)))
    n = len(_ANCHORS)

    def tbl(key_name: str, anchors: list[str | None]) -> pa.Table:
        return pa.table(
            {
                key_name: pa.array([f"p{i}" for i in range(n)], type=pa.string()),
                "dob": pa.array(anchors, type=pa.string()),
                "event": pa.array([None] * n, type=pa.string()),
            }
        )

    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(
                (
                    "parent",
                    TableSeed(
                        per_column=(("pk", _KEY), ("dob", anchor_seed), ("event", wd)),
                        per_group=(),
                    ),
                ),
                (
                    "child",
                    TableSeed(
                        per_column=(("fk", _KEY), ("dob", anchor_seed), ("event", wd)),
                        per_group=(),
                    ),
                ),
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
    return plan, {"parent": tbl("pk", _ANCHORS), "child": tbl("fk", list(reversed(_ANCHORS)))}, graph


def _gate(plan: Any, graph: RelationshipGraph):
    work = order_work(build_work_list(plan, _REG), graph)
    return check_out_of_core_compatibility(plan, work, graph)


def _assert_value_equal(oracle: pa.Table, ooc: pa.Table, label: str) -> None:
    got, want = ooc.to_pydict(), oracle.to_pydict()
    assert set(got) == set(want), f"{label}: columns {set(got)} vs {set(want)}"
    for name in want:
        assert got[name] == want[name], (
            f"{label}: {name!r} diverges\n oracle={want[name]}\n    ooc={got[name]}"
        )


@pytest.mark.parametrize("batch_rows", [None, 4, 3, 2, 1])
def test_windowed_date_payload_parity(batch_rows: int | None) -> None:
    """windowed_date on parent + child is byte-identical to the oracle at every
    batch size: the durable row ordinal makes each row key on its whole-source
    position, so the output does not depend on the batch boundary."""
    plan, sources, graph = _job(_PASSTHROUGH)
    assert _gate(plan, graph).accepted, _gate(plan, graph).message()
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    ooc = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, batch_rows=batch_rows
    )
    for table in oracle.outputs:
        _assert_value_equal(oracle.outputs[table], ooc.outputs[table], f"batch={batch_rows}")


def test_windowed_date_output_is_batch_size_invariant() -> None:
    """Belt-and-braces: a 1-row-per-batch run equals a whole-table run, proving
    the offset tracks the global position rather than restarting per batch."""
    plan, sources, graph = _job(_PASSTHROUGH)
    whole = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    sliced = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, batch_rows=1
    )
    for table in whole.outputs:
        _assert_value_equal(whole.outputs[table], sliced.outputs[table], f"invariant:{table}")


def test_masked_anchor_declines() -> None:
    """A windowed_date whose anchor is masked (not unmasked/passthrough) is a
    gate MISS with an actionable code, so it falls back to full-frame rather
    than risk a column-order divergence."""
    plan, _sources, graph = _job(_seed("hash", namespace="dobns"))
    verdict = _gate(plan, graph)
    assert not verdict.accepted
    assert "out_of_core_windowed_date_masked_anchor_unsupported" in {
        r.code for r in verdict.rejections
    }


def _run_sink(plan: Any, sources: Any, graph: Any, target: Path) -> bool:
    saw_reorder = False
    orig_reorder = StreamFkJoiner.__init__

    def spy(self: Any, *a: Any, **k: Any) -> None:
        nonlocal saw_reorder
        saw_reorder = True
        orig_reorder(self, *a, **k)

    StreamFkJoiner.__init__ = spy  # type: ignore[method-assign]
    try:
        run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            sink=ParquetTransactionalSink(target),
            temp_dir=target.parent / f"{target.name}-work",
            budget_bytes=1024 * _MIB,
            temp_disk_budget_bytes=200 * 1024**3,
            out_of_core_reorder_threshold_rows=0,
        )
    finally:
        StreamFkJoiner.__init__ = orig_reorder  # type: ignore[method-assign]
    return saw_reorder


def test_windowed_date_reorder_driver_parity(tmp_path: Path) -> None:
    """The child's windowed_date masks through the REORDER driver's Phase-1
    bridge; its sink output stays byte-identical to the oracle."""
    plan, sources, graph = _job(_PASSTHROUGH)
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    out = tmp_path / "out"
    assert _run_sink(plan, sources, graph, out), "threshold_rows=0 should force the reorder driver"
    for name in ("parent", "child"):
        _assert_value_equal(oracle.outputs[name], pq.read_table(out / f"{name}.parquet"), name)
