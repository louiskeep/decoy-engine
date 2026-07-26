"""engine-v2 S9 slice 2h: orphan policy + FK resolution + composite-FK groups.

The adapter masks an FK parent and its child in ONE multi-table `run` call. The
child FK column resolves against the parent's in-run source->masked map (so
referential integrity holds by construction); a child row with no parent is an
orphan, handled per the edge's `OrphanPolicy`. Composite-key FK children resolve
the same way with tuple keys, after the parent's per-column scalar nodes mask.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, PandasExecutionAdapter
from decoy_engine.execution._strategies._orphan import (
    cascade_row_errors,
    gather_errored_parent_keys,
    make_remap_fn,
    resolve_fk_keys,
)
from decoy_engine.plan._types import ColumnSeed, GroupSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceBinding, NamespaceRegistry

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_SEED = (0xABCD).to_bytes(8, "big")


def _hash_col(namespace: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _run(plan: Any, sources: dict[str, pa.Table], graph: RelationshipGraph) -> Any:
    return PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )


# --------------------------------------------------------------------------
# Single-column FK + the four orphan policies.
# --------------------------------------------------------------------------


def _single_fk_plan() -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "customers",
                    TableSeed(per_column=(("customer_id", _hash_col("cust")),), per_group=()),
                ),
                (
                    "orders",
                    TableSeed(per_column=(("customer_id", _hash_col("cust")),), per_group=()),
                ),
            ),
        )
    )


def _single_fk_graph(policy: OrphanPolicy) -> RelationshipGraph:
    edge = RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="cust",
        orphan_policy=policy,
    )
    return RelationshipGraph(edges=(edge,), ordering=())


def _single_fk_sources() -> dict[str, pa.Table]:
    # c9 is an orphan: it is not in customers.
    return {
        "customers": pa.table({"customer_id": ["c1", "c2", "c3"]}),
        "orders": pa.table({"customer_id": ["c1", "c2", "c1", "c9"]}),
    }


class TestSingleColumnOrphanPolicy:
    def test_baseline_referential_integrity(self) -> None:
        res = _run(_single_fk_plan(), _single_fk_sources(), _single_fk_graph(OrphanPolicy.PRESERVE))
        parent = res.outputs["customers"].column("customer_id").to_pylist()
        child = res.outputs["orders"].column("customer_id").to_pylist()
        pmap = {"c1": parent[0], "c2": parent[1], "c3": parent[2]}
        assert child[0] == pmap["c1"]  # non-orphan rows map to the masked parent
        assert child[1] == pmap["c2"]
        assert child[2] == pmap["c1"]  # repeated FK -> same masked value
        assert parent[0] != "c1"  # the parent actually masked

    def test_preserve(self) -> None:
        res = _run(_single_fk_plan(), _single_fk_sources(), _single_fk_graph(OrphanPolicy.PRESERVE))
        child = res.outputs["orders"].column("customer_id").to_pylist()
        assert child[3] == "c9"  # orphan kept unmasked
        assert res.warnings == ()

    def test_remap(self) -> None:
        res = _run(_single_fk_plan(), _single_fk_sources(), _single_fk_graph(OrphanPolicy.REMAP))
        child = res.outputs["orders"].column("customer_id").to_pylist()
        assert child[3] != "c9"  # orphan got a fresh masked value
        assert child[3] is not None

    def test_warn(self) -> None:
        res = _run(_single_fk_plan(), _single_fk_sources(), _single_fk_graph(OrphanPolicy.WARN))
        child = res.outputs["orders"].column("customer_id").to_pylist()
        assert child[3] == "c9"  # preserved
        codes = [w.code for w in res.warnings]
        assert codes.count("orphan_fk") == 1  # aggregated, not one-per-row
        assert res.warnings[0].detail["orphan_rows"] == 1

    def test_fail(self) -> None:
        with pytest.raises(ExecutionError) as exc:
            _run(_single_fk_plan(), _single_fk_sources(), _single_fk_graph(OrphanPolicy.FAIL))
        assert exc.value.code == "orphan_fk_violation"

    def test_null_fk_preserved_not_orphan(self) -> None:
        sources = {
            "customers": pa.table({"customer_id": ["c1", "c2"]}),
            "orders": pa.table({"customer_id": ["c1", None, "c2"]}),
        }
        # FAIL would raise if null were treated as an orphan; it must not.
        res = _run(_single_fk_plan(), sources, _single_fk_graph(OrphanPolicy.FAIL))
        child = res.outputs["orders"].column("customer_id").to_pylist()
        assert child[1] is None


# --------------------------------------------------------------------------
# Composite-key FK: parent PK columns mask as scalars, child tuple resolves
# through the parent tuple map (RI for the whole tuple).
# --------------------------------------------------------------------------

_COMPOSITE_COLS = ("member_id", "plan_id", "effective_date")


def _composite_plan() -> Any:
    parent_cols = tuple((c, _hash_col(f"enr_{c}")) for c in _COMPOSITE_COLS)
    group = GroupSeed(namespace="enr", coherent_columns=_COMPOSITE_COLS)
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("enrollments", TableSeed(per_column=parent_cols, per_group=())),
                ("claims", TableSeed(per_column=(), per_group=(("member_id__plan_id", group),))),
            ),
        )
    )


def _composite_graph(policy: OrphanPolicy) -> RelationshipGraph:
    edge = RelationshipEdge(
        parent_table="enrollments",
        parent_columns=_COMPOSITE_COLS,
        child_table="claims",
        child_columns=_COMPOSITE_COLS,
        namespace="enr",
        orphan_policy=policy,
    )
    return RelationshipGraph(edges=(edge,), ordering=())


class TestCompositeFkGroup:
    def test_child_tuple_resolves_to_parent_masked_tuple(self) -> None:
        sources = {
            "enrollments": pa.table(
                {
                    "member_id": ["m1", "m2"],
                    "plan_id": ["p1", "p2"],
                    "effective_date": ["2020", "2021"],
                }
            ),
            "claims": pa.table(
                {
                    "member_id": ["m2", "m1"],
                    "plan_id": ["p2", "p1"],
                    "effective_date": ["2021", "2020"],
                }
            ),
        }
        res = _run(_composite_plan(), sources, _composite_graph(OrphanPolicy.FAIL))
        enr = res.outputs["enrollments"]
        claims = res.outputs["claims"]
        # Build masked parent tuples keyed by source tuple.
        parent_masked = {
            ("m1", "p1", "2020"): (
                enr.column("member_id")[0].as_py(),
                enr.column("plan_id")[0].as_py(),
                enr.column("effective_date")[0].as_py(),
            ),
            ("m2", "p2", "2021"): (
                enr.column("member_id")[1].as_py(),
                enr.column("plan_id")[1].as_py(),
                enr.column("effective_date")[1].as_py(),
            ),
        }
        # claims row 0 referenced (m2,p2,2021); row 1 referenced (m1,p1,2020).
        claim_row0 = (
            claims.column("member_id")[0].as_py(),
            claims.column("plan_id")[0].as_py(),
            claims.column("effective_date")[0].as_py(),
        )
        claim_row1 = (
            claims.column("member_id")[1].as_py(),
            claims.column("plan_id")[1].as_py(),
            claims.column("effective_date")[1].as_py(),
        )
        assert claim_row0 == parent_masked[("m2", "p2", "2021")]
        assert claim_row1 == parent_masked[("m1", "p1", "2020")]
        # The parent tuple actually masked (not identity).
        assert parent_masked[("m1", "p1", "2020")] != ("m1", "p1", "2020")

    def test_composite_orphan_fail_raises(self) -> None:
        sources = {
            "enrollments": pa.table(
                {"member_id": ["m1"], "plan_id": ["p1"], "effective_date": ["2020"]}
            ),
            "claims": pa.table(
                {"member_id": ["m9"], "plan_id": ["p9"], "effective_date": ["2099"]}
            ),
        }
        with pytest.raises(ExecutionError) as exc:
            _run(_composite_plan(), sources, _composite_graph(OrphanPolicy.FAIL))
        assert exc.value.code == "orphan_fk_violation"


# --------------------------------------------------------------------------
# Multi-table run contract (PQ-S9-C).
# --------------------------------------------------------------------------


class TestMultiTableContract:
    def test_outputs_carry_every_table(self) -> None:
        res = _run(_single_fk_plan(), _single_fk_sources(), _single_fk_graph(OrphanPolicy.PRESERVE))
        assert set(res.outputs) == {"customers", "orders"}
        assert res.boundary_conversion_ms >= 0.0

    def test_output_property_raises_for_multi_table(self) -> None:
        res = _run(_single_fk_plan(), _single_fk_sources(), _single_fk_graph(OrphanPolicy.PRESERVE))
        with pytest.raises(ExecutionError) as exc:
            _ = res.output
        assert exc.value.code == "multi_table_result_has_no_single_output"


# --------------------------------------------------------------------------
# F2 (Dennis verify): integer FK whose child column has a null -> pandas upcasts
# the child to float64 while the parent stays int64. The non-null child rows
# must still resolve (not silently orphan). _fk_key_value normalizes both sides.
# --------------------------------------------------------------------------


class TestIntegerFkDtypeCoercion:
    def test_int_parent_float_child_still_resolves(self) -> None:
        sources = {
            "customers": pa.table({"customer_id": [1, 2, 3]}),  # int64, no null
            "orders": pa.table({"customer_id": [1, None, 2]}),  # null -> float64 upcast
        }
        res = _run(_single_fk_plan(), sources, _single_fk_graph(OrphanPolicy.PRESERVE))
        parent = res.outputs["customers"].column("customer_id").to_pylist()
        child = res.outputs["orders"].column("customer_id").to_pylist()
        pmap = {1: parent[0], 2: parent[1], 3: parent[2]}
        assert child[0] == pmap[1]  # int 1 (parent) matches float 1.0 (child)
        assert child[1] is None  # null preserved
        assert child[2] == pmap[2]
        assert res.warnings == ()  # nothing orphaned by a dtype mismatch


# --------------------------------------------------------------------------
# F1 (Dennis verify, highest-priority slice-3 item): R17 composite-as-FK-parent
# RUNTIME. A composite OUTPUT column (people.email) is a FK parent referenced by
# a child (logins.email). The composite must mask first (R17 ordering), record
# its parent map, and the child resolves against the masked composite output.
# --------------------------------------------------------------------------

_NE_COLS = ("email", "first_name", "last_name")


def _composite_col(coherent_with: tuple[str, ...]) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="<composite>",
        provider="composite_name_email",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=coherent_with,
    )


def _r17_plan() -> Any:
    people = TableSeed(
        per_column=(
            ("first_name", _composite_col(("last_name", "email"))),
            ("last_name", _composite_col(("first_name", "email"))),
            ("email", _composite_col(("first_name", "last_name"))),
        ),
        per_group=(),
    )
    logins = TableSeed(per_column=(("email", _hash_col("login_ns")),), per_group=())
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("people", people), ("logins", logins)),
        )
    )


def _r17_graph(policy: OrphanPolicy) -> RelationshipGraph:
    edge = RelationshipEdge(
        parent_table="people",
        parent_columns=("email",),
        child_table="logins",
        child_columns=("email",),
        namespace="login_ns",
        orphan_policy=policy,
    )
    return RelationshipGraph(edges=(edge,), ordering=())


def _r17_ns() -> NamespaceRegistry:
    # The composite whole-tuple binding the composite handler resolves.
    group = tuple(sorted(_NE_COLS))
    return NamespaceRegistry(
        bindings=(NamespaceBinding(namespace="ne_ns", declared_by=(("people", group),)),)
    )


def _run_r17(plan: Any, sources: dict[str, pa.Table], graph: RelationshipGraph) -> Any:
    return PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_r17_ns()
    )


class TestR17CompositeAsFkParent:
    def test_child_resolves_against_masked_composite_output(self) -> None:
        sources = {
            "people": pa.table(
                {
                    "first_name": ["Anna", "Bob"],
                    "last_name": ["Lee", "Kim"],
                    "email": ["anna@x.com", "bob@y.com"],
                }
            ),
            "logins": pa.table({"email": ["anna@x.com", "ghost@z.com"]}),
        }
        res = _run_r17(_r17_plan(), sources, _r17_graph(OrphanPolicy.PRESERVE))
        people_email = res.outputs["people"].column("email").to_pylist()
        login_email = res.outputs["logins"].column("email").to_pylist()
        # The composite actually masked the parent email (R17: composite ran first).
        assert people_email[0] != "anna@x.com"
        # The non-orphan child resolves to the masked composite output column.
        assert login_email[0] == people_email[0]
        # The orphan is preserved per policy.
        assert login_email[1] == "ghost@z.com"

    def test_orphan_policy_applies_on_top_of_r17(self) -> None:
        sources = {
            "people": pa.table(
                {"first_name": ["Anna"], "last_name": ["Lee"], "email": ["anna@x.com"]}
            ),
            "logins": pa.table({"email": ["ghost@z.com"]}),  # orphan only
        }
        with pytest.raises(ExecutionError) as exc:
            _run_r17(_r17_plan(), sources, _r17_graph(OrphanPolicy.FAIL))
        assert exc.value.code == "orphan_fk_violation"


# --------------------------------------------------------------------------
# Direct unit coverage of the resolver helpers. The adapter tests above never
# reach the S2 EXCLUDE-then-CASCADE path (no row-errored parents) nor the
# REMAP-failure branches, so the referential-integrity invariants for those
# paths are pinned here against their machine-observable outputs.
# --------------------------------------------------------------------------


def _edge(
    policy: OrphanPolicy,
    *,
    parent_columns: tuple[str, ...] = ("customer_id",),
    child_columns: tuple[str, ...] = ("customer_id",),
    namespace: str = "cust",
) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="customers",
        parent_columns=parent_columns,
        child_table="orders",
        child_columns=child_columns,
        namespace=namespace,
        orphan_policy=policy,
    )


def _resolve(
    child_keys: list[Any],
    parent_map: dict[Any, Any],
    edge: RelationshipEdge,
    *,
    remap_fn: Any = None,
    errored: dict[Any, str] | None = None,
) -> Any:
    if remap_fn is None:
        remap_fn = list  # identity remap; only the REMAP policy consults it
    return resolve_fk_keys(
        child_keys, parent_map, edge, remap_fn=remap_fn, errored_parent_keys=errored
    )


class TestCascadePrecedence:
    def test_errored_parent_key_cascades_to_none_with_trigger(self) -> None:
        # A key present only as an excluded (row-errored) parent key is a
        # cascade: masked value is None (never the raw key) and the row is
        # recorded as (index, trigger) for a downstream RowError.
        masked, warnings, cascade = _resolve(
            [("k1",)], {}, _edge(OrphanPolicy.PRESERVE), errored={("k1",): "format_error"}
        )
        assert masked[0] is None
        assert cascade == [(0, "format_error")]
        assert warnings == []

    def test_cascade_does_not_halt_later_rows(self) -> None:
        # A cascaded row must not stop processing of the rows after it: a
        # genuine orphan on a later row still gets the orphan policy applied.
        masked, _warnings, cascade = _resolve(
            [("k1",), ("orphan",)],
            {},
            _edge(OrphanPolicy.PRESERVE),
            errored={("k1",): "format_error"},
        )
        assert cascade == [(0, "format_error")]
        assert masked[1] == ("orphan",)

    def test_parent_map_hit_takes_precedence_over_errored_key(self) -> None:
        # Precedence (1): a parent_map hit resolves normally even when the same
        # key also appears in errored_parent_keys; it does not cascade.
        masked, _warnings, cascade = _resolve(
            [("k1",)],
            {("k1",): ("masked1",)},
            _edge(OrphanPolicy.PRESERVE),
            errored={("k1",): "format_error"},
        )
        assert masked[0] == ("masked1",)
        assert cascade == []


class TestRemapFailClosed:
    def test_short_remap_result_fails_closed(self) -> None:
        # The REMAP zip is strict: a remap_fn that returns fewer values than
        # orphans must fail loudly rather than silently drop an orphan remap.
        with pytest.raises(ValueError):
            _resolve([("o1",)], {}, _edge(OrphanPolicy.REMAP), remap_fn=lambda keys: [])


class TestWarnEventShape:
    def test_warn_emits_single_aggregated_warning(self) -> None:
        masked, warnings, cascade = _resolve([("o1",), ("o2",)], {}, _edge(OrphanPolicy.WARN))
        assert len(warnings) == 1
        w = warnings[0]
        assert w.code == "orphan_fk"
        assert w.provider == "cust"
        assert w.column == "customer_id"
        assert w.detail == {
            "parent_table": "customers",
            "parent_columns": ["customer_id"],
            "child_table": "orders",
            "child_columns": ["customer_id"],
            "orphan_rows": 2,
        }
        assert masked == [("o1",), ("o2",)]
        assert cascade == []

    def test_warn_column_joins_composite_child_columns(self) -> None:
        edge = _edge(
            OrphanPolicy.WARN,
            parent_columns=("member_id", "plan_id"),
            child_columns=("member_id", "plan_id"),
            namespace="enr",
        )
        _masked, warnings, _cascade = _resolve([("m9", "p9")], {}, edge)
        assert warnings[0].column == "member_id,plan_id"
        assert warnings[0].detail["child_columns"] == ["member_id", "plan_id"]
        assert warnings[0].detail["parent_columns"] == ["member_id", "plan_id"]


class TestGatherErroredParentKeys:
    def test_none_cache_returns_empty_dict(self) -> None:
        result = gather_errored_parent_keys((_edge(OrphanPolicy.PRESERVE),), None)
        assert result == {}
        assert isinstance(result, dict)

    def test_collects_keys_with_their_triggers(self) -> None:
        cache = {("customers", ("customer_id",)): {("c1",): "format_error"}}
        result = gather_errored_parent_keys((_edge(OrphanPolicy.PRESERVE),), cache)
        assert result == {("c1",): "format_error"}

    def test_absent_cache_key_contributes_nothing(self) -> None:
        # An edge whose parent node has no errored keys must resolve to an
        # empty contribution, not blow up on a missing cache entry.
        cache = {("other", ("x",)): {("z",): "mask_error"}}
        result = gather_errored_parent_keys((_edge(OrphanPolicy.PRESERVE),), cache)
        assert result == {}

    def test_first_hit_wins_across_edges(self) -> None:
        cache = {
            ("customers", ("customer_id",)): {("c1",): "format_error"},
            ("customers2", ("customer_id",)): {("c1",): "mask_error"},
        }
        edges = (
            _edge(OrphanPolicy.PRESERVE),
            RelationshipEdge(
                parent_table="customers2",
                parent_columns=("customer_id",),
                child_table="orders",
                child_columns=("customer_id",),
                namespace="cust",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        )
        result = gather_errored_parent_keys(edges, cache)
        assert result == {("c1",): "format_error"}


class TestCascadeRowErrors:
    def test_builds_one_row_error_per_cascaded_row(self) -> None:
        errors = cascade_row_errors([(3, "format_error"), (7, "mask_error")], "customer_id")
        assert len(errors) == 2
        assert [(e.column, e.row_index, e.trigger) for e in errors] == [
            ("customer_id", 3, "format_error"),
            ("customer_id", 7, "mask_error"),
        ]


class _RecordingHandler:
    """Minimal StrategyHandler stand-in that records the table in force when
    it runs and returns a deterministically-masked column."""

    def __init__(self) -> None:
        self.seen_tables: list[Any] = []

    def run(self, df: Any, col: str, plan_slice: Any, ctx: Any) -> Any:
        self.seen_tables.append(ctx.current_table)
        df[col] = ["m_" + str(v) for v in df[col]]
        return df, None


class TestMakeRemapFn:
    def test_missing_parent_node_fails_closed(self) -> None:
        remap = make_remap_fn(
            _edge(OrphanPolicy.REMAP), {}, SimpleNamespace(current_table=None), {}
        )
        with pytest.raises(ExecutionError) as exc:
            remap([("c9",)])
        assert exc.value.code == "orphan_remap_parent_missing"

    def test_missing_handler_fails_closed(self) -> None:
        node = SimpleNamespace(plan_slice=_hash_col("cust"), strategy="nope")
        node_by_key = {("customers", ("customer_id",)): node}
        remap = make_remap_fn(
            _edge(OrphanPolicy.REMAP), node_by_key, SimpleNamespace(current_table=None), {}
        )
        with pytest.raises(ExecutionError) as exc:
            remap([("c9",)])
        assert exc.value.code == "unsupported_strategy"

    def test_parent_table_is_set_during_run_then_restored(self) -> None:
        node = SimpleNamespace(plan_slice=_hash_col("cust"), strategy="rec")
        node_by_key = {("customers", ("customer_id",)): node}
        handler = _RecordingHandler()
        ctx = SimpleNamespace(current_table="orders")  # child dispatch in flight
        remap = make_remap_fn(_edge(OrphanPolicy.REMAP), node_by_key, ctx, {"rec": handler})
        result = remap([("c9",)])
        assert result == [("m_c9",)]
        assert handler.seen_tables == ["customers"]  # remapped via the PARENT table
        assert ctx.current_table == "orders"  # prior table restored afterward
