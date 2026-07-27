"""Mutation-kill suite for `PandasExecutionAdapter._resolve_fk_node` and
`._parent_map` -- the FK parent-map resolution / RI cluster.

Every test drives `PandasExecutionAdapter().run(...)` directly and asserts a
machine-observable RI outcome: the masked child FK bytes, the orphan
disposition (preserve/remap/fail), the S2 EXCLUDE-then-CASCADE result (a child
of a row-errored parent key masks to None with a cascaded RowError), lossless
integer typing, or a coded error. No wall-clock assertions (tq-findings #18).

Scenario vocabulary used below:
  * S2A  -- a parent FK KEY column masked by `date_shift` carries one
            uncoercible value; that key row-errors, is EXCLUDED from the parent
            map, and any child referencing it CASCADES to None + a RowError.
  * B    -- a parent NON-key column errors while the key column is clean
            (exercises the per-column `tbl_errs.get(c, {})` lookup).
  * C    -- two independent parent/child pairs where one pair's parent errors,
            so `key_error_rows` is truthy while the OTHER pair's parent is
            absent from it (exercises `key_error_rows.get(ptable, {})`).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, GroupSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_SEED = (0xABCD).to_bytes(8, "big")


def _col(strategy: str, namespace: str | None, pc: tuple[tuple[str, Any], ...] = ()) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=pc,
        coherent_with=(),
    )


def _hash(ns: str) -> ColumnSeed:
    return _col("hash", ns)


def _plan(per_table: tuple[tuple[str, TableSeed], ...]) -> Any:
    return SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=_SEED, per_table=per_table))


def _run(plan: Any, sources: dict[str, pa.Table], graph: RelationshipGraph) -> Any:
    return PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )


def _g1(edge: RelationshipEdge) -> RelationshipGraph:
    return RelationshipGraph(edges=(edge,), ordering=())


def _edge(
    parent: str,
    child: str,
    ns: str,
    policy: OrphanPolicy,
    *,
    parent_columns: tuple[str, ...] = ("id",),
    child_columns: tuple[str, ...] = ("parent_id",),
) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table=parent,
        parent_columns=parent_columns,
        child_table=child,
        child_columns=child_columns,
        namespace=ns,
        orphan_policy=policy,
    )


# ===========================================================================
# S2A: a row-errored FK KEY column excludes its key from the parent map, and
# a child referencing it cascades to None (never the raw key). The bad row is
# placed FIRST so that a `break`-for-`continue` mutation in the exclude/null
# loop (which would skip every later parent row) is observable via the good
# rows failing to resolve.
# ===========================================================================


def _s2a_plan() -> Any:
    return _plan(
        (
            ("parent", TableSeed(per_column=(("id", _col("date_shift", "pns")),), per_group=())),
            ("child", TableSeed(per_column=(("parent_id", _hash("pns")),), per_group=())),
        )
    )


def _s2a_sources() -> dict[str, pa.Table]:
    # Row 0 ("notadate") row-errors; rows 1 and 2 are clean dates. The child
    # references the bad key (row 0) plus both good keys.
    return {
        "parent": pa.table({"id": ["notadate", "2020-01-01", "2020-02-01"]}),
        "child": pa.table({"parent_id": ["notadate", "2020-01-01", "2020-02-01"]}),
    }


def test_s2a_errored_parent_key_cascades_child_to_none_and_good_rows_resolve() -> None:
    res = _run(
        _s2a_plan(), _s2a_sources(), _g1(_edge("parent", "child", "pns", OrphanPolicy.PRESERVE))
    )
    parent_out = res.outputs["parent"].column("id").to_pylist()
    child = res.outputs["child"].column("parent_id").to_pylist()

    # The bad key is EXCLUDED from the map and its child row cascades to None
    # (kills the leak mutants: excluded/errored keyed wrong, key_error_rows or
    # errored_keys_cache dropped, gather passed None).
    assert child[0] is None
    # The good rows AFTER the excluded row still resolve to the masked parent
    # value -- a `break` instead of `continue` in the exclude branch would drop
    # them from the map and orphan them (PRESERVE would keep the raw date).
    assert parent_out[1] != "2020-01-01"  # date_shift actually masked it
    assert child[1] == parent_out[1]
    assert child[2] == parent_out[2]
    assert "2020-01-01" not in child  # no raw key leaked onto the child


def test_s2a_child_cascade_row_error_carries_column_and_trigger() -> None:
    res = _run(
        _s2a_plan(), _s2a_sources(), _g1(_edge("parent", "child", "pns", OrphanPolicy.PRESERVE))
    )
    child_errs = [r for r in res.row_errors if r.table == "child"]
    # Exactly one cascaded child RowError, attributed to the child FK column,
    # carrying the SAME trigger as the parent's key-error (kills the mutants
    # that null the excluded/errored trigger value or the cascade column).
    assert len(child_errs) == 1
    assert child_errs[0].column == "parent_id"
    assert child_errs[0].trigger == "format_error"


# ===========================================================================
# Scenario B: a parent NON-key column row-errors while the key column is
# clean, so `key_error_rows[parent]` is truthy but the key column is absent
# from it -- the per-column `tbl_errs.get(c, {})` must yield {} (not crash).
# ===========================================================================


def test_scenario_b_clean_key_resolves_when_nonkey_column_errored() -> None:
    plan = _plan(
        (
            (
                "parent",
                TableSeed(
                    per_column=(
                        ("id", _hash("pns")),
                        ("age", _col("bucketize", None, (("width", 10),))),
                    ),
                    per_group=(),
                ),
            ),
            ("child", TableSeed(per_column=(("parent_id", _hash("pns")),), per_group=())),
        )
    )
    sources = {
        "parent": pa.table({"id": ["k1", "k2"], "age": ["10", "badX"]}),
        "child": pa.table({"parent_id": ["k1", "k2"]}),
    }
    res = _run(plan, sources, _g1(_edge("parent", "child", "pns", OrphanPolicy.PRESERVE)))
    parent_id = res.outputs["parent"].column("id").to_pylist()
    child = res.outputs["child"].column("parent_id").to_pylist()
    # The clean FK key resolves normally; a `tbl_errs.get(c, None)` /
    # `tbl_errs.get(c)` mutation would look up the absent key column and crash
    # on `None.items()`.
    assert child == parent_id
    assert child[0] != "k1"  # actually masked


# ===========================================================================
# Scenario C: two independent parent/child pairs. The first pair's parent
# row-errors (so `key_error_rows` is truthy) while the second pair's parent is
# NEVER in it -- `key_error_rows.get(ptable, {})` must return {} rather than
# `None` (which a dropped default would crash on).
# ===========================================================================


def test_scenario_c_independent_clean_pair_resolves_beside_errored_pair() -> None:
    plan = _plan(
        (
            ("errpar", TableSeed(per_column=(("id", _col("date_shift", "ea")),), per_group=())),
            ("errchild", TableSeed(per_column=(("pid", _hash("ea")),), per_group=())),
            ("cleanpar", TableSeed(per_column=(("id", _hash("cb")),), per_group=())),
            ("cleanchild", TableSeed(per_column=(("pid", _hash("cb")),), per_group=())),
        )
    )
    edges = (
        _edge("errpar", "errchild", "ea", OrphanPolicy.PRESERVE, child_columns=("pid",)),
        _edge("cleanpar", "cleanchild", "cb", OrphanPolicy.PRESERVE, child_columns=("pid",)),
    )
    sources = {
        "errpar": pa.table({"id": ["2020-01-01", "notadate"]}),
        "errchild": pa.table({"pid": ["2020-01-01"]}),
        "cleanpar": pa.table({"id": ["x1", "x2"]}),
        "cleanchild": pa.table({"pid": ["x1", "x2"]}),
    }
    res = _run(plan, sources, RelationshipGraph(edges=edges, ordering=()))
    clean_par = res.outputs["cleanpar"].column("id").to_pylist()
    clean_child = res.outputs["cleanchild"].column("pid").to_pylist()
    # The clean pair's map is built while `key_error_rows` already holds the
    # errored pair's rows but NOT "cleanpar"; a dropped `{}` default on the
    # per-table lookup crashes on `None.get(...)`.
    assert clean_child == clean_par
    assert clean_child[0] != "x1"  # actually masked


# ===========================================================================
# Null parent-key component: a parent row with a null key cannot be
# referenced and is skipped (`continue`); a `break` there would drop every
# LATER parent row from the map. The good key sits AFTER the null row.
# ===========================================================================


def test_null_parent_key_row_skipped_not_break() -> None:
    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("id", _hash("d")),), per_group=())),
            ("child", TableSeed(per_column=(("parent_id", _hash("d")),), per_group=())),
        )
    )
    sources = {
        "parent": pa.table({"id": pa.array([None, "k1", "k2"], type=pa.string())}),
        "child": pa.table({"parent_id": ["k1"]}),
    }
    res = _run(plan, sources, _g1(_edge("parent", "child", "d", OrphanPolicy.PRESERVE)))
    parent_id = res.outputs["parent"].column("id").to_pylist()
    child = res.outputs["child"].column("parent_id").to_pylist()
    # "k1" (a row AFTER the null parent row) must resolve to its masked value,
    # not be orphaned by an early `break`.
    assert child[0] == parent_id[1]
    assert child[0] != "k1"


# ===========================================================================
# Parent absent from frames: an edge whose parent table is not among the
# sources returns an empty map (child fully orphaned/preserved). Exercises the
# `ptable not in frames` early-return branch and its errored_keys_cache write.
# ===========================================================================


def test_parent_absent_from_frames_preserves_child_keys() -> None:
    plan = _plan((("child", TableSeed(per_column=(("parent_id", _hash("pns")),), per_group=())),))
    graph = _g1(_edge("parent", "child", "pns", OrphanPolicy.PRESERVE))
    res = _run(plan, {"child": pa.table({"parent_id": ["x1", "x2"]})}, graph)
    # Parent absent -> empty parent map -> every child key preserved. A mutant
    # that stores `None` (not `{}`) into errored_keys_cache here crashes the
    # subsequent gather (`None.items()`).
    assert res.outputs["child"].column("parent_id").to_pylist() == ["x1", "x2"]


# ===========================================================================
# Cache-key collision: a 3-table chain has two edges with DISTINCT parent
# tables. A `cache_key = None` mutation makes the second edge read the first
# edge's cached map, mis-resolving the grandchild.
# ===========================================================================


def test_three_table_chain_grandchild_resolves_against_its_own_parent() -> None:
    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("id", _hash("nsp")),), per_group=())),
            (
                "child",
                TableSeed(
                    per_column=(("id", _hash("nsc")), ("parent_id", _hash("nsp"))), per_group=()
                ),
            ),
            ("grandchild", TableSeed(per_column=(("child_id", _hash("nsc")),), per_group=())),
        )
    )
    edges = (
        _edge("parent", "child", "nsp", OrphanPolicy.PRESERVE, child_columns=("parent_id",)),
        _edge(
            "child",
            "grandchild",
            "nsc",
            OrphanPolicy.PRESERVE,
            parent_columns=("id",),
            child_columns=("child_id",),
        ),
    )
    sources = {
        "parent": pa.table({"id": ["p0", "p1"]}),
        "child": pa.table({"id": ["c0", "c1"], "parent_id": ["p0", "p1"]}),
        "grandchild": pa.table({"child_id": ["c0", "c1"]}),
    }
    res = _run(plan, sources, RelationshipGraph(edges=edges, ordering=()))
    child_id = res.outputs["child"].column("id").to_pylist()
    gc = res.outputs["grandchild"].column("child_id").to_pylist()
    # The grandchild FK must resolve against the CHILD's masked id map, not a
    # cache entry keyed as None that collides with the parent edge's map.
    assert gc == child_id
    assert gc[0] != "c0"  # actually masked, not a preserved orphan


# ===========================================================================
# Multi-parent (two parents, one shared key): first-hit-wins precedence and a
# child resolvable ONLY via the second parent. Exercises the whole
# `len(edges) > 1` merge branch and every argument of the extra `_parent_map`
# call.
# ===========================================================================


def _multiparent_plan() -> Any:
    return _plan(
        (
            ("pa", TableSeed(per_column=(("id", _hash("nsa")),), per_group=())),
            (
                "pb",
                TableSeed(
                    per_column=(("id", _col("fpe", "nsb", (("charset", "alphanum"),))),),
                    per_group=(),
                ),
            ),
            ("child", TableSeed(per_column=(("id", _hash("nsa")),), per_group=())),
        )
    )


def _multiparent_graph(policy: OrphanPolicy) -> RelationshipGraph:
    edges = tuple(
        _edge(parent, "child", ns, policy, child_columns=("id",))
        for parent, ns in (("pa", "nsa"), ("pb", "nsb"))
    )
    return RelationshipGraph(edges=edges, ordering=())


def _multiparent_sources() -> dict[str, pa.Table]:
    return {
        "pa": pa.table({"id": ["a1", "shared1"]}),
        "pb": pa.table({"id": ["b1", "shared1"]}),
        "child": pa.table({"id": ["a1", "b1", "shared1", "ghost"]}),
    }


def test_multiparent_resolves_through_both_parents_with_precedence() -> None:
    res = _run(
        _multiparent_plan(), _multiparent_sources(), _multiparent_graph(OrphanPolicy.PRESERVE)
    )
    out_a = res.outputs["pa"].column("id").to_pylist()
    out_b = res.outputs["pb"].column("id").to_pylist()
    child = res.outputs["child"].column("id").to_pylist()

    assert child[0] == out_a[0]  # a1 resolves through parent pa
    # b1 exists ONLY in pb: the merge branch (edges[1:], the extra _parent_map
    # call, and merged.setdefault) must run for this to resolve.
    assert child[1] == out_b[0]
    assert child[1] != "b1"
    # shared1 is in BOTH parents with different masked values; first declared
    # parent wins.
    assert out_a[1] != out_b[1]
    assert child[2] == out_a[1]
    # ghost is in neither -> preserved orphan.
    assert child[3] == "ghost"


# ===========================================================================
# Multi-parent where the SECOND parent's key column row-errors: the extra
# `_parent_map` call must thread `key_error_rows` / `errored_keys_cache` so the
# child referencing that errored key CASCADES to None (never the raw key).
# ===========================================================================


def test_multiparent_second_parent_errored_key_cascades_child_to_none() -> None:
    plan = _plan(
        (
            ("pa", TableSeed(per_column=(("id", _hash("nsa")),), per_group=())),
            ("pb", TableSeed(per_column=(("id", _col("date_shift", "nsb")),), per_group=())),
            ("child", TableSeed(per_column=(("id", _hash("nsa")),), per_group=())),
        )
    )
    edges = tuple(
        _edge(parent, "child", ns, OrphanPolicy.PRESERVE, child_columns=("id",))
        for parent, ns in (("pa", "nsa"), ("pb", "nsb"))
    )
    sources = {
        "pa": pa.table({"id": ["a1"]}),
        "pb": pa.table({"id": ["2020-01-01", "notadate"]}),
        "child": pa.table({"id": ["a1", "notadate"]}),
    }
    res = _run(plan, sources, RelationshipGraph(edges=edges, ordering=()))
    child = res.outputs["child"].column("id").to_pylist()
    # "notadate" lives only in the SECOND parent and is row-errored there; if
    # the extra _parent_map call drops key_error_rows/errored_keys_cache, the
    # child leaks the raw key instead of cascading to None.
    assert child[1] is None
    assert "notadate" not in child


# ===========================================================================
# Composite FK (multi-column key): the null-mask must be computed across ALL
# key columns (`.any(axis=1)`), and the >1 arity guard must pick the composite
# branch for a 2-column key. A child row null in only the SECOND column is a
# null FK (both output cells None), not a resolvable key.
# ===========================================================================

_COMPOSITE_COLS = ("member_id", "plan_id")


def _composite_plan() -> Any:
    parent_cols = tuple((c, _hash(f"e_{c}")) for c in _COMPOSITE_COLS)
    group = GroupSeed(namespace="enr", coherent_columns=_COMPOSITE_COLS)
    return _plan(
        (
            ("enr", TableSeed(per_column=parent_cols, per_group=())),
            ("claims", TableSeed(per_column=(), per_group=(("member_id__plan_id", group),))),
        )
    )


def _composite_graph(policy: OrphanPolicy) -> RelationshipGraph:
    edge = _edge(
        "enr",
        "claims",
        "enr",
        policy,
        parent_columns=_COMPOSITE_COLS,
        child_columns=_COMPOSITE_COLS,
    )
    return RelationshipGraph(edges=(edge,), ordering=())


def test_composite_fk_null_in_second_column_is_null_fk_not_orphan() -> None:
    sources = {
        "enr": pa.table({"member_id": ["m1"], "plan_id": ["p1"]}),
        # Row 0 matches; row 1 has a null in the SECOND key column only.
        "claims": pa.table({"member_id": ["m1", "m1"], "plan_id": ["p1", None]}),
    }
    res = _run(_composite_plan(), sources, _composite_graph(OrphanPolicy.PRESERVE))
    enr = res.outputs["enr"]
    claims = res.outputs["claims"]
    m_out = claims.column("member_id").to_pylist()
    p_out = claims.column("plan_id").to_pylist()
    # Row 0 resolves to the masked parent tuple (composite branch selected).
    assert (m_out[0], p_out[0]) == (
        enr.column("member_id")[0].as_py(),
        enr.column("plan_id")[0].as_py(),
    )
    assert m_out[0] != "m1"
    # Row 1 is a NULL FK across the tuple -> both cells None. A per-column
    # null-mask mutation (only col0, or a broken axis) would treat (m1, None)
    # as a real key and orphan it, preserving the raw "m1".
    assert m_out[1] is None
    assert p_out[1] is None


# ===========================================================================
# Orphan policies: PRESERVE keeps the raw key, REMAP mints a fresh masked
# value via the parent strategy. REMAP with an orphan exercises the remap_fn
# construction; PRESERVE with an orphan exercises edge access in the resolver.
# ===========================================================================


def _single_plan() -> Any:
    return _plan(
        (
            ("cust", TableSeed(per_column=(("id", _hash("c")),), per_group=())),
            ("ord", TableSeed(per_column=(("parent_id", _hash("c")),), per_group=())),
        )
    )


def _single_sources() -> dict[str, pa.Table]:
    # c9 is a genuine orphan (absent from cust).
    return {
        "cust": pa.table({"id": ["c1", "c2"]}),
        "ord": pa.table({"parent_id": ["c1", "c9"]}),
    }


def test_preserve_orphan_keeps_raw_key() -> None:
    res = _run(
        _single_plan(), _single_sources(), _g1(_edge("cust", "ord", "c", OrphanPolicy.PRESERVE))
    )
    ord_out = res.outputs["ord"].column("parent_id").to_pylist()
    cust = res.outputs["cust"].column("id").to_pylist()
    assert ord_out[0] == cust[0]  # matched
    assert ord_out[1] == "c9"  # orphan preserved (edge policy consulted)


def test_remap_orphan_mints_fresh_masked_value() -> None:
    res = _run(
        _single_plan(), _single_sources(), _g1(_edge("cust", "ord", "c", OrphanPolicy.REMAP))
    )
    ord_out = res.outputs["ord"].column("parent_id").to_pylist()
    cust = res.outputs["cust"].column("id").to_pylist()
    assert ord_out[0] == cust[0]  # matched
    # The orphan is remapped via the parent strategy: not the raw key, not None.
    assert ord_out[1] != "c9"
    assert ord_out[1] is not None


# ===========================================================================
# Lossless integer FK typing (DE-10 write-back contract): a resolved integer
# FK column must build through the nullable-int path, never a raw list
# assignment that rounds a key beyond 2**53 to float64.
# ===========================================================================

_BIG_KEY = 9007199254740993  # 2**53 + 1: does not round-trip through float64


def _int_plan() -> Any:
    seed = _col("passthrough", "d")
    return _plan(
        (
            ("parent", TableSeed(per_column=(("pk", seed),), per_group=())),
            ("child", TableSeed(per_column=(("fk", seed),), per_group=())),
        )
    )


def _int_graph() -> RelationshipGraph:
    return RelationshipGraph(
        edges=(
            _edge(
                "parent",
                "child",
                "d",
                OrphanPolicy.PRESERVE,
                parent_columns=("pk",),
                child_columns=("fk",),
            ),
        ),
        ordering=(),
    )


def test_big_int_fk_survives_exact_beside_null() -> None:
    sources = {
        "parent": pa.table({"pk": pa.array([1, _BIG_KEY], type=pa.int64())}),
        "child": pa.table({"fk": pa.array([1, None, _BIG_KEY], type=pa.int64())}),
    }
    out = _run(_int_plan(), sources, _int_graph()).outputs["child"].column("fk")
    # A raw list write-back (safe_ints dropped) would infer float64 and round
    # _BIG_KEY; the all(...) guard mutation would crash or null the column.
    assert out.type == pa.int64()
    assert out.to_pylist() == [1, None, _BIG_KEY]


def test_int_fk_no_null_is_not_forced_all_null() -> None:
    sources = {
        "parent": pa.table({"pk": pa.array([1, _BIG_KEY], type=pa.int64())}),
        "child": pa.table({"fk": pa.array([1, _BIG_KEY], type=pa.int64())}),
    }
    out = _run(_int_plan(), sources, _int_graph()).outputs["child"].column("fk")
    # An inverted `all(v is None)` guard would route this fully-resolved column
    # into the all-null builder.
    assert out.to_pylist() == [1, _BIG_KEY]


def test_all_null_resolved_fk_preserves_source_width() -> None:
    sources = {
        "parent": pa.table({"pk": pa.array([1, 2], type=pa.uint32())}),
        "child": pa.table({"fk": pa.array([None, None], type=pa.uint32())}),
    }
    out = _run(_int_plan(), sources, _int_graph()).outputs["child"].column("fk")
    # The all-null branch preserves the column's own uint32 dtype (and must not
    # crash on a dropped length/dtype argument, nor scalar-None assign).
    assert out.type == pa.uint32()
    assert out.to_pylist() == [None, None]
