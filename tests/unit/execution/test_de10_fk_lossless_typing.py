"""DE-10: lossless FK integer-typing contract, parametrized across routes.

Root cause (see `execution/_fk_keys.py` module docstring for the full
write-up): pandas has no lossless numpy representation for an integer column
that contains a null -- `to_pandas()` (ingestion) and a raw Python-list column
assignment (FK write-back) both fall back to float64 (NaN for the null), and
float64 cannot exactly hold an integer beyond 2**53. Before this fix, that
made the full-frame/sequential (pandas-backed) route SILENTLY ROUND a large FK
key while the out-of-core route already failed closed
(`out_of_core_fk_key_dtype_unsupported`) on the one shape it cannot resolve --
a route-dependent correctness gap on referential-integrity data (documented as
an accepted, deliberately-out-of-scope divergence in
tests/parity/SEMANTIC_DIFFERENCES.md prior to DE-10).

These tests pin the contract directly (not just via `test_out_of_core_fk_parity.py`'s
existing property suite, extended alongside this fix): a key beyond 2**53
either survives byte-exact on every route, or every route raises the identical
`ExecutionError(code="out_of_core_fk_key_dtype_unsupported")`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, PandasExecutionAdapter
from decoy_engine.execution._fk_keys import FK_KEY_DTYPE_UNSUPPORTED_CODE
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_JOB_SEED = b"\x22" * 8

# Beyond this magnitude, IEEE-754 double precision (53-bit mantissa) cannot
# represent every integer exactly -- the boundary this whole contract is about.
_BIG_KEY = 9007199254740993  # 2**53 + 1, odd: does NOT round-trip through float64


def _seed(strategy: str = "passthrough", *, namespace: str | None = "de10") -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=(),
        coherent_with=(),
    )


def _plan(per_table: tuple[tuple[str, TableSeed], ...]) -> Any:
    return SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=_JOB_SEED, per_table=per_table))


def _edge(policy: OrphanPolicy) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="parent",
        parent_columns=("pk",),
        child_table="child",
        child_columns=("fk",),
        namespace="de10",
        orphan_policy=policy,
    )


def _graph(policy: OrphanPolicy) -> RelationshipGraph:
    return RelationshipGraph(edges=(_edge(policy),), ordering=())


def _plan_for(parent_col: str = "pk", child_col: str = "fk") -> Any:
    seed = _seed()
    return _plan(
        (
            ("parent", TableSeed(per_column=((parent_col, seed),), per_group=())),
            ("child", TableSeed(per_column=((child_col, seed),), per_group=())),
        )
    )


def _loader(sources: dict[str, pa.Table]):
    def load(table: str) -> pa.Table:
        return sources[table]

    return load


def _run_full_frame(plan, sources, graph) -> pa.Table:
    result = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    return result.outputs["child"].column("fk")


def _run_sequential(plan, sources, graph) -> pa.Table:
    result = PandasExecutionAdapter().run_sequential(
        plan,
        _loader(sources),
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )
    return result.outputs["child"].column("fk")


def _run_out_of_core(plan, sources, graph) -> pa.Table:
    result = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    return result.outputs["child"].column("fk")


_ROUTES = {
    "full_frame": _run_full_frame,
    "sequential": _run_sequential,
    "out_of_core": _run_out_of_core,
}


# ---------------------------------------------------------------------------
# Exact survival: a matched large key beside a null in the SAME output column
# (the write-back shape: `_resolve_fk_node`'s raw list assignment used to
# infer float64 the moment a None sat next to a big int).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_large_matched_key_survives_exact_beside_a_null(route: str) -> None:
    parent = pa.table({"pk": pa.array([1, _BIG_KEY], type=pa.int64())})
    # Row 0 matches parent 1; row 1 is a null FK (preserved as null, never an
    # orphan); row 2 matches parent's big key exactly.
    child = pa.table({"fk": pa.array([1, None, _BIG_KEY], type=pa.int64())})
    plan = _plan_for()
    graph = _graph(OrphanPolicy.PRESERVE)
    sources = {"parent": parent, "child": child}

    out = _ROUTES[route](plan, sources, graph)
    assert out.type == pa.int64(), f"{route}: expected int64, got {out.type}"
    assert out.to_pylist() == [1, None, _BIG_KEY], f"{route}: key was not preserved exactly"


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_large_orphan_key_survives_exact_beside_a_null(route: str) -> None:
    """Ingestion-side half of the fix: the CHILD's own raw source column is
    null-bearing and carries the big key on an orphan row (no matching parent
    at all), so PRESERVE reads it straight off the source -- if ingestion
    (`to_pandas()`/`to_pandas_fk_safe`) had already rounded it, no write-back
    fix could recover the value."""
    parent = pa.table({"pk": pa.array([1, 2], type=pa.int64())})
    child = pa.table({"fk": pa.array([_BIG_KEY, None], type=pa.int64())})
    plan = _plan_for()
    graph = _graph(OrphanPolicy.PRESERVE)
    sources = {"parent": parent, "child": child}

    out = _ROUTES[route](plan, sources, graph)
    assert out.type == pa.int64(), f"{route}: expected int64, got {out.type}"
    assert out.to_pylist() == [_BIG_KEY, None], f"{route}: orphan key was not preserved exactly"


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_large_key_survives_exact_with_no_nulls(route: str) -> None:
    """Sanity: a null-free big-key column was never rounded (int64 has no
    float fallback without a null); pin it stays that way after the fix."""
    parent = pa.table({"pk": pa.array([1, _BIG_KEY], type=pa.int64())})
    child = pa.table({"fk": pa.array([1, _BIG_KEY], type=pa.int64())})
    plan = _plan_for()
    graph = _graph(OrphanPolicy.PRESERVE)
    sources = {"parent": parent, "child": child}

    out = _ROUTES[route](plan, sources, graph)
    assert out.type == pa.int64()
    assert out.to_pylist() == [1, _BIG_KEY]


# ---------------------------------------------------------------------------
# Route parity: the SAME job produces byte-identical FK key columns everywhere.
# ---------------------------------------------------------------------------


def test_route_parity_fk_key_columns_byte_identical() -> None:
    parent = pa.table({"pk": pa.array([1, 2, _BIG_KEY], type=pa.int64())})
    child = pa.table({"fk": pa.array([1, None, _BIG_KEY, 999], type=pa.int64())})
    plan = _plan_for()
    graph = _graph(OrphanPolicy.PRESERVE)
    sources = {"parent": parent, "child": child}

    full = _run_full_frame(plan, sources, graph)
    seq = _run_sequential(plan, sources, graph)
    ooc = _run_out_of_core(plan, sources, graph)

    assert full.equals(seq), "full_frame vs sequential FK column diverges"
    assert full.equals(ooc), "full_frame vs out_of_core FK column diverges"
    assert full.to_pylist() == [1, None, _BIG_KEY, 999]


# ---------------------------------------------------------------------------
# Fail-pre reproduction of the root cause (pinned independently of the fixed
# adapter, direct pandas/pyarrow repro -- proves the mechanism this contract
# closes, not just its absence post-fix).
# ---------------------------------------------------------------------------


def test_bare_to_pandas_still_rounds_a_nullable_int_column() -> None:
    """This is NOT a regression test for the engine -- it pins the underlying
    pandas/pyarrow behavior `to_pandas_fk_safe` exists to route around, so a
    pyarrow/pandas upgrade that changed this default would be caught here
    rather than silently reopening the ingestion half of DE-10."""
    tbl = pa.table({"k": pa.array([_BIG_KEY, None, 5], type=pa.int64())})
    df = tbl.to_pandas()
    assert df["k"].dtype == "float64"
    assert df["k"].tolist()[0] != _BIG_KEY  # rounded


def test_bare_list_assignment_still_rounds_a_nullable_int_column() -> None:
    df = pd.DataFrame({"x": [0, 0, 0]})
    df["x"] = [1, _BIG_KEY, None]
    assert df["x"].dtype == "float64"
    assert df["x"].tolist()[1] != _BIG_KEY  # rounded


# ---------------------------------------------------------------------------
# Unrepresentable mix: every route raises the SAME typed error, never a
# silently-picked lossy dtype.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.WARN])
def test_unrepresentable_mix_raises_same_code_every_route(route: str, policy: OrphanPolicy) -> None:
    """A literal float matched parent value beside an orphan integer key
    beyond exact float precision: no dtype can hold both losslessly, so every
    route must fail closed with the identical code (never round, never
    crash with an uncoded error)."""
    parent = pa.table({"pk": pa.array([1.0, 2.0], type=pa.float64())})
    child = pa.table({"fk": pa.array([1, _BIG_KEY], type=pa.int64())})
    plan = _plan_for()
    graph = _graph(policy)
    sources = {"parent": parent, "child": child}

    with pytest.raises(ExecutionError) as exc:
        _ROUTES[route](plan, sources, graph)
    assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE == "out_of_core_fk_key_dtype_unsupported"
