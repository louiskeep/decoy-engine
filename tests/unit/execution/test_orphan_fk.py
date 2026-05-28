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
from decoy_engine.plan._types import ColumnSeed, GroupSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

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
