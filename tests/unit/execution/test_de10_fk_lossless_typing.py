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

The contract covers signed AND unsigned AND narrower-than-int64 FK keys, not
"int64 only": `test_unsigned_key_past_int64_survives_exact_beside_a_null`
pins a `uint64` key in `[2**63, 2**64)` (unreachable through a single blanket
signed `Int64` cast -- the pandas routes raised an uncoded
`pyarrow.lib.ArrowInvalid`/`OverflowError` for this shape under that cast, a
route-dependent divergence caught in re-review and closed alongside the
original 2**53 finding), and `test_narrow_int_key_column_preserves_width`
pins that a narrower source dtype (`int32`) is not silently widened to
`int64` in the output (a schema change a blanket `Int64` cast also caused).
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

# Beyond this magnitude, signed Int64 cannot represent the value at all (its
# max is 2**63 - 1) -- the boundary HIGH #2's unsigned-key fix is about. Only
# a genuine uint64 column can carry a value this large.
_UNSIGNED_BIG_KEY = 9223372036854775813  # 2**63 + 5

# A narrow signed width well below int16's range, used to prove int32 output
# width is preserved (not widened to int64) rather than exercise precision.
_NARROW_INT32_VALUE = 300_000


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

# `uint64` FK-column tests below deliberately exclude out_of_core: it rejects
# EVERY uint64-typed relationship key column unconditionally, regardless of
# magnitude (`out_of_core/_batch_join.py::_python_roundtrip_type` -- "its
# Python round trip is value-dependent"), a pre-existing, deliberate,
# documented design choice ("A compatibility rejection beats byte drift")
# unrelated to the 2**53 precision story DE-10 is about; it is NOT part of
# this fix's scope and is pinned separately below
# (`test_out_of_core_rejects_any_uint64_fk_column_unconditionally`).
_ROUTES_PANDAS_ONLY = {"full_frame": _run_full_frame, "sequential": _run_sequential}


def _run_full_frame_table(plan, sources, graph, *, table: str, column: str) -> pa.Array:
    result = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    return result.outputs[table].column(column)


def _run_sequential_table(plan, sources, graph, *, table: str, column: str) -> pa.Array:
    result = PandasExecutionAdapter().run_sequential(
        plan,
        _loader(sources),
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )
    return result.outputs[table].column(column)


def _run_out_of_core_table(plan, sources, graph, *, table: str, column: str) -> pa.Array:
    result = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    return result.outputs[table].column(column)


_ROUTES_TABLE = {
    "full_frame": _run_full_frame_table,
    "sequential": _run_sequential_table,
    "out_of_core": _run_out_of_core_table,
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
# Width & signedness preservation (dennis+Codex remediation: BLOCKER #1,
# HIGH #2, MEDIUM #3). An unconditional blanket signed Int64 cast (the
# initial DE-10 rework cut) widened a narrower FK key's output dtype and
# could not represent an unsigned key >= 2**63 at all; the exact
# per-Arrow-type mapper closes both.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", sorted(_ROUTES_PANDAS_ONLY))
def test_unsigned_key_past_int64_survives_exact_beside_a_null(route: str) -> None:
    """HIGH #2: a genuine uint64 FK key >= 2**63 (unsigned snowflake/
    bigserial ID) must survive byte-exact, never crash with an uncoded
    pyarrow.lib.ArrowInvalid/OverflowError the way a blanket signed Int64
    cast did (pinned pre-fix by
    `test_bare_int64_cast_still_overflows_a_uint64_key_past_int64_range`
    below). out_of_core is excluded here -- see `_ROUTES_PANDAS_ONLY`."""
    parent = pa.table({"pk": pa.array([1, _UNSIGNED_BIG_KEY], type=pa.uint64())})
    child = pa.table({"fk": pa.array([1, None, _UNSIGNED_BIG_KEY], type=pa.uint64())})
    plan = _plan_for()
    graph = _graph(OrphanPolicy.PRESERVE)
    sources = {"parent": parent, "child": child}

    out = _ROUTES_PANDAS_ONLY[route](plan, sources, graph)
    assert out.type == pa.uint64(), f"{route}: expected uint64, got {out.type}"
    assert out.to_pylist() == [1, None, _UNSIGNED_BIG_KEY], f"{route}: key not preserved exactly"


def test_out_of_core_rejects_any_uint64_fk_column_unconditionally() -> None:
    """Pins out_of_core's PRE-EXISTING, unrelated limitation (not part of
    this fix, not a regression it introduces): it refuses ANY uint64-typed
    FK column, even one whose every value is small -- so the pandas routes'
    new uint64 support (above) is not full three-route parity for this
    dtype. See `_ROUTES_PANDAS_ONLY`'s comment and this file's/CHANGELOG's
    remediation notes for the tracked-follow-up framing."""
    parent = pa.table({"pk": pa.array([1, 2], type=pa.uint64())})
    child = pa.table({"fk": pa.array([1, 2], type=pa.uint64())})
    plan = _plan_for()
    graph = _graph(OrphanPolicy.PRESERVE)
    sources = {"parent": parent, "child": child}

    with pytest.raises(ExecutionError) as exc:
        _run_out_of_core(plan, sources, graph)
    assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_narrow_int_key_column_preserves_width(route: str) -> None:
    """BLOCKER #1 / MEDIUM #3: a narrow-width FK key (e.g. an int32
    auto-increment PK) must not silently widen to int64 in the output -- the
    CHANGELOG's original claim that this fix "does not change any existing
    job's output bytes" was FALSE for this shape under the initial blanket-
    Int64 cast (pinned pre-fix by
    `test_bare_blanket_int64_cast_still_widens_a_narrower_int_column`
    below). Checks the PARENT's own PK column (not the CHILD's FK column,
    which is rebuilt fresh at write-back for every route -- including
    out_of_core, whose own FK-child output is unconditionally widened to
    int64 by a separate, pre-existing, documented design
    (`out_of_core/_batch_join.py::_python_roundtrip_type`), unrelated to and
    out of scope for this fix)."""
    parent = pa.table({"pk": pa.array([1, None, _NARROW_INT32_VALUE], type=pa.int32())})
    child = pa.table({"fk": pa.array([1, _NARROW_INT32_VALUE], type=pa.int32())})
    plan = _plan_for()
    graph = _graph(OrphanPolicy.PRESERVE)
    sources = {"parent": parent, "child": child}

    out = _ROUTES_TABLE[route](plan, sources, graph, table="parent", column="pk")
    assert out.type == pa.int32(), f"{route}: expected int32 preserved, got {out.type}"
    assert out.to_pylist() == [1, None, _NARROW_INT32_VALUE]


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


def test_bare_int64_cast_still_overflows_a_uint64_key_past_int64_range() -> None:
    """Pins the underlying pyarrow behavior a blanket signed Int64 cast (the
    initial DE-10 rework cut, before the exact per-Arrow-type mapper) could
    not route around: a genuine uint64 key >= 2**63 raises an UNCODED
    `pyarrow.lib.ArrowInvalid` when forced through a nullable Int64 (signed)
    extension dtype -- not a silent rounding, an uncoded crash, which is
    exactly HIGH #2 (`to_pandas_fk_safe`'s `_exact_fk_types_mapper` routes
    around this by casting a uint64 column to UInt64, not Int64)."""
    tbl = pa.table({"k": pa.array([1, _UNSIGNED_BIG_KEY, None], type=pa.uint64())})
    with pytest.raises(pa.lib.ArrowInvalid):
        tbl.column("k").to_pandas(types_mapper=lambda _t: pd.Int64Dtype())


def test_bare_blanket_int64_cast_still_widens_a_narrower_int_column() -> None:
    """Pins the underlying pyarrow behavior a blanket signed Int64 cast
    forced on EVERY null-bearing integer FK column regardless of its own
    width: a genuine int32 column widens to a pandas Int64 (which
    round-trips to Arrow int64) -- a schema change for a job whose FK key
    was never anywhere near float64's precision limit (BLOCKER #1 / MEDIUM
    #3). `to_pandas_fk_safe`'s exact per-Arrow-type mapper routes around
    this by casting an int32 column to Int32 (its own matching width)."""
    tbl = pa.table({"k": pa.array([1, None, _NARROW_INT32_VALUE], type=pa.int32())})
    widened = tbl.column("k").to_pandas(types_mapper=lambda _t: pd.Int64Dtype())
    assert str(widened.dtype) == "Int64"  # widened from the source's int32


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
