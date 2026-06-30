"""Regression net for the multi-table FK relationships mask path.

The committed perf tiers are a single flat table, so they never run the
`relationships` full-frame path that the engine takes when a config declares
foreign keys. This module exercises that path at a small, fast scale: a parent to
child to grandchild chain built by `tests/perf_fixtures/fk_relational.py`, masked
through `PandasExecutionAdapter.run`. It guards two things that must not regress:
FK referential integrity across the chain, and a loose memory ceiling on the
full-frame path.

The heavy scaling measurement (hundreds of thousands to millions of rows) lives in
`scripts/fk_memory_probe.py`, which is run by hand on an adequate-RAM box, not in
this default gate. Keep this test small enough to stay well under the per-test
budget (a few thousand rows per table).

Source: docs/relationships-memory-scaling.md (FK-RI memory-scaling, Phase 0).
"""

from __future__ import annotations

import tracemalloc

import pytest

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.relationships._graph import OrphanPolicy
from tests.perf_fixtures.fk_relational import build_fk_relational

pytestmark = pytest.mark.perf

# Small enough to mask in well under a second on the dev box, large enough that an
# accidental full-frame blowup (e.g. a per-row copy) would move the tracemalloc
# number. Budget is a loose ceiling, not a tight calibration.
_ROWS = 4_000
_WIDTH = 4
_ORPHAN_FRAC = 0.05
_MEM_BUDGET_MB = 64


def _mask(fixture, policy):
    return PandasExecutionAdapter().run(
        fixture.plan,
        fixture.sources,
        registry=fixture.registry,
        relationship_graph=fixture.graph(policy),
        namespace_registry=fixture.namespace_registry,
    )


def test_fk_chain_preserves_referential_integrity():
    """Every non-orphan FK row across a 2-edge chain resolves to the masked
    parent key, and parents are actually masked."""
    fixture = build_fk_relational(rows=_ROWS, width=_WIDTH, orphan_frac=_ORPHAN_FRAC)
    res = _mask(fixture, OrphanPolicy.PRESERVE)

    for parent_t, child_t, fk_col in (
        ("parent", "child", "parent_id"),
        ("child", "grandchild", "child_id"),
    ):
        p_src = fixture.sources[parent_t].column("id").to_pylist()
        p_msk = res.outputs[parent_t].column("id").to_pylist()
        pmap = dict(zip(p_src, p_msk, strict=True))
        c_src = fixture.sources[child_t].column(fk_col).to_pylist()
        c_msk = res.outputs[child_t].column(fk_col).to_pylist()
        assert p_src[0] != p_msk[0], f"{parent_t}.id was not masked"
        for s, m in zip(c_src, c_msk, strict=True):
            if s in pmap:
                assert m == pmap[s], f"{child_t}.{fk_col} FK broken for {s}"
            else:
                assert m == s, f"{child_t}.{fk_col} orphan {s} not preserved raw"


def test_fk_orphan_policies_behave():
    """remap mints fresh values for orphans; fail aborts."""
    fixture = build_fk_relational(rows=_ROWS, width=_WIDTH, orphan_frac=_ORPHAN_FRAC)

    remapped = _mask(fixture, OrphanPolicy.REMAP)
    src = fixture.sources["child"].column("parent_id").to_pylist()
    out = remapped.outputs["child"].column("parent_id").to_pylist()
    parents = set(fixture.sources["parent"].column("id").to_pylist())
    orphan_outputs = [o for s, o in zip(src, out, strict=True) if s not in parents]
    assert orphan_outputs, "fixture planted no orphans"
    assert all(o is not None for o in orphan_outputs)

    from decoy_engine.execution import ExecutionError

    with pytest.raises(ExecutionError) as exc:
        _mask(fixture, OrphanPolicy.FAIL)
    assert exc.value.code == "orphan_fk_violation"


def test_full_frame_memory_under_budget():
    """Sentinel: the full-frame FK mask at a small scale stays under a loose
    Python-object memory ceiling. Catches a regression that balloons per-row
    allocation. The real scaling numbers come from scripts/fk_memory_probe.py."""
    fixture = build_fk_relational(rows=_ROWS, width=_WIDTH, orphan_frac=_ORPHAN_FRAC)
    tracemalloc.start()
    _mask(fixture, OrphanPolicy.PRESERVE)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_mb = peak / (1024 * 1024)
    assert peak_mb < _MEM_BUDGET_MB, (
        f"full-frame FK mask peak {peak_mb:.1f} MiB exceeds {_MEM_BUDGET_MB} MiB "
        f"budget at {_ROWS} rows x 3 tables"
    )
