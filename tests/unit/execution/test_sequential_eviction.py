"""Option 2 (FK-RI memory-scaling): sequential load+mask+evict byte-parity.

`run_sequential` masks an FK-related job one table at a time in FK-topological
order, loading each table lazily, building the narrow parent key-maps that
downstream children need, then evicting the parent's wide frame. Its contract is
byte-identical output to the full-frame `run`, for every orphan policy. These
tests pin that parity (the whole point of Option 2 is lower memory, not different
bytes) on a generated multi-table chain and on the hand-built orphan cases.

Source: docs/relationships-memory-scaling.md section 4 + 6.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, PandasExecutionAdapter
from decoy_engine.relationships._graph import OrphanPolicy
from tests.perf_fixtures.fk_relational import build_fk_relational


def _loader(sources: dict[str, pa.Table]):
    def load(table: str) -> pa.Table:
        return sources[table]

    return load


def _full_frame(adapter, plan, sources, graph, ns, registry):
    return adapter.run(
        plan, sources, registry=registry, relationship_graph=graph, namespace_registry=ns
    )


def _sequential(adapter, plan, sources, graph, ns, registry, sink=None):
    return adapter.run_sequential(
        plan,
        _loader(sources),
        registry=registry,
        relationship_graph=graph,
        namespace_registry=ns,
        sink=sink,
    )


@pytest.mark.parametrize(
    "policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP, OrphanPolicy.WARN]
)
def test_byte_parity_with_full_frame(policy):
    """Sequential output equals full-frame output, column for column, for each
    non-aborting orphan policy, across the parent->child->grandchild chain."""
    adapter = PandasExecutionAdapter()
    fx = build_fk_relational(rows=3_000, width=3, orphan_frac=0.05)
    graph = fx.graph(policy)

    full = _full_frame(adapter, plan := fx.plan, fx.sources, graph, fx.namespace_registry, fx.registry)
    seq = _sequential(adapter, plan, fx.sources, graph, fx.namespace_registry, fx.registry)

    assert set(seq.outputs) == set(full.outputs)
    for table in full.outputs:
        assert seq.outputs[table].equals(full.outputs[table]), f"{table} differs"
    # Warnings (e.g. WARN orphan aggregate) must match too.
    assert {(w.code, w.detail.get("orphan_rows")) for w in seq.warnings} == {
        (w.code, w.detail.get("orphan_rows")) for w in full.warnings
    }


def test_fail_policy_aborts_in_both_paths():
    adapter = PandasExecutionAdapter()
    fx = build_fk_relational(rows=1_000, width=2, orphan_frac=0.05)
    graph = fx.graph(OrphanPolicy.FAIL)
    for run in (
        lambda: _full_frame(adapter, fx.plan, fx.sources, graph, fx.namespace_registry, fx.registry),
        lambda: _sequential(adapter, fx.plan, fx.sources, graph, fx.namespace_registry, fx.registry),
    ):
        with pytest.raises(ExecutionError) as exc:
            run()
        assert exc.value.code == "orphan_fk_violation"


def test_sink_streams_each_table_and_collect_is_empty():
    """With a sink, outputs are streamed out per table (not accumulated), so the
    returned outputs are empty and the sink saw every table exactly once."""
    adapter = PandasExecutionAdapter()
    fx = build_fk_relational(rows=500, width=2, orphan_frac=0.0)
    seen: dict[str, pa.Table] = {}

    def sink(table: str, out: pa.Table) -> None:
        assert table not in seen, f"{table} emitted twice"
        seen[table] = out

    res = _sequential(
        adapter, fx.plan, fx.sources, fx.graph(OrphanPolicy.PRESERVE),
        fx.namespace_registry, fx.registry, sink=sink,
    )
    assert set(seen) == set(fx.sources)
    assert res.outputs == {}

    # Sink output equals full-frame output.
    full = _full_frame(
        adapter, fx.plan, fx.sources, fx.graph(OrphanPolicy.PRESERVE),
        fx.namespace_registry, fx.registry,
    )
    for table in full.outputs:
        assert seen[table].equals(full.outputs[table]), f"{table} sink differs"
