"""`fk_out_of_core_dtype_hazard`: would out-of-core reject this job's FK key types?

The signal is the schema-aware half of the retention rule in `decide_execution_route`.
Each case here is checked against the real out-of-core joiner in
`tests/parity/test_gap6_prefer_bounded_fk.py` (the shapes it flags really do reject
there, and the shapes it clears really do run), so a false negative would show up
as a failing end-to-end run, not only as a wrong boolean.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._pipeline_routing_fk_dtype import fk_out_of_core_dtype_hazard
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.profile._readers import LazySource
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

_BIG = 2**53 + 1


def _seed(strategy: str) -> ColumnSeed:
    return ColumnSeed(
        namespace="ns",
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _plan(strategy: str, *, parent_seeded: bool = True) -> Any:
    parent_cols = (("pk", _seed(strategy)),) if parent_seeded else ()
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x01" * 8,
            per_table=(
                ("parent", TableSeed(per_column=parent_cols, per_group=())),
                ("child", TableSeed(per_column=(("fk", _seed(strategy)),), per_group=())),
            ),
        )
    )


def _graph(policy: OrphanPolicy = OrphanPolicy.PRESERVE) -> RelationshipGraph:
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=("pk",),
        child_table="child",
        child_columns=("fk",),
        namespace="ns",
        orphan_policy=policy,
    )
    return RelationshipGraph(edges=(edge,), ordering=())


def _sources(parent: pa.Array, child: pa.Array) -> dict[str, Any]:
    return {"parent": pa.table({"pk": parent}), "child": pa.table({"fk": child})}


def _hazard(
    parent: pa.Array,
    child: pa.Array,
    *,
    strategy: str = "passthrough",
    policy: OrphanPolicy = OrphanPolicy.PRESERVE,
) -> bool:
    return fk_out_of_core_dtype_hazard(
        _plan(strategy), _graph(policy), caller_sources=_sources(parent, child)
    )


_UTC = pa.timestamp("us", tz="UTC")
_T = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)

# (parent, child): key shapes out-of-core rejects while the oracle completes them.
_REJECTED = {
    "decimal": (
        pa.array([Decimal(1)], pa.decimal128(10, 0)),
        pa.array([Decimal(1)], pa.decimal128(10, 0)),
    ),
    "uint64": (pa.array([1], pa.uint64()), pa.array([1], pa.uint64())),
    "tz_timestamp": (pa.array([_T], _UTC), pa.array([_T], _UTC)),
    "date64": (
        pa.array([dt.date(2020, 1, 1)], pa.date64()),
        pa.array([dt.date(2020, 1, 1)], pa.date64()),
    ),
    "time64": (pa.array([dt.time(1)], pa.time64("us")), pa.array([dt.time(1)], pa.time64("us"))),
    "duration": (pa.array([1], pa.duration("us")), pa.array([1], pa.duration("us"))),
    "fixed_size_binary": (pa.array([b"ab"], pa.binary(2)), pa.array([b"ab"], pa.binary(2))),
    "dictionary": (pa.array(["a"]).dictionary_encode(), pa.array(["a"]).dictionary_encode()),
    "string_parent_binary_child": (pa.array(["a"]), pa.array([b"a"], pa.binary())),
    "float_parent_int_child": (pa.array([1.5]), pa.array([_BIG], pa.int64())),
    "bool_parent_int_child": (pa.array([True]), pa.array([1], pa.int64())),
}

# (parent, child): shapes out-of-core types without a data-dependent step.
_CLEARED = {
    "int64": (pa.array([1], pa.int64()), pa.array([1], pa.int64())),
    "narrow_ints": (pa.array([1], pa.int32()), pa.array([1], pa.int16())),
    "string": (pa.array(["a"]), pa.array(["a"])),
    "large_string_child": (pa.array(["a"]), pa.array(["a"], pa.large_string())),
    "binary": (pa.array([b"a"]), pa.array([b"a"])),
    "float": (pa.array([1.5]), pa.array([1.5])),
    "date32": (
        pa.array([dt.date(2020, 1, 1)], pa.date32()),
        pa.array([dt.date(2020, 1, 1)], pa.date32()),
    ),
    "naive_timestamp": (pa.array([_T.replace(tzinfo=None)]), pa.array([_T.replace(tzinfo=None)])),
}


class TestRejectedShapesAreFlagged:
    @pytest.mark.parametrize("name", sorted(_REJECTED))
    @pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP])
    def test_flagged(self, name: str, policy: OrphanPolicy) -> None:
        parent, child = _REJECTED[name]
        assert _hazard(parent, child, policy=policy) is True

    def test_bool_keys_are_flagged_unless_orphans_are_impossible(self) -> None:
        # An orphan's bool key normalizes to int64, so the fixed type depends on the data.
        parent, child = pa.array([True]), pa.array([False])
        assert _hazard(parent, child) is True
        assert _hazard(parent, child, policy=OrphanPolicy.FAIL) is False

    def test_a_decimal_child_key_is_flagged_under_a_hashed_parent(self) -> None:
        # The masked parent type is string, so the hazard comes from the orphan candidate
        # alone (the child's own key type), not from the parent.
        parent, child = pa.array([1, 2], pa.int64()), pa.array([Decimal(1)], pa.decimal128(10, 0))
        assert _hazard(parent, child, strategy="hash") is True


class TestSupportedShapesAreCleared:
    @pytest.mark.parametrize("name", sorted(_CLEARED))
    def test_cleared(self, name: str) -> None:
        parent, child = _CLEARED[name]
        assert _hazard(parent, child) is False

    def test_hashed_string_keys_are_cleared(self) -> None:
        assert _hazard(pa.array(["a"]), pa.array(["a"]), strategy="hash") is False


class TestUnknownOrUntypeableIsConservative:
    def test_a_table_without_a_schema_is_a_hazard(self) -> None:
        sources = {"parent": pa.table({"pk": pa.array([1])})}
        assert fk_out_of_core_dtype_hazard(_plan("passthrough"), _graph(), caller_sources=sources)

    def test_a_missing_key_column_is_a_hazard(self) -> None:
        sources = {"parent": pa.table({"other": pa.array([1])}), "child": pa.table({"fk": [1]})}
        assert fk_out_of_core_dtype_hazard(_plan("passthrough"), _graph(), caller_sources=sources)

    def test_remap_without_a_parent_seed_is_a_hazard(self) -> None:
        plan = _plan("passthrough", parent_seeded=False)
        sources = _sources(pa.array([1]), pa.array([1]))
        assert fk_out_of_core_dtype_hazard(plan, _graph(OrphanPolicy.REMAP), caller_sources=sources)

    def test_an_unseeded_parent_key_is_typed_as_passthrough(self) -> None:
        plan = _plan("passthrough", parent_seeded=False)
        sources = _sources(pa.array([1]), pa.array([1]))
        assert not fk_out_of_core_dtype_hazard(plan, _graph(), caller_sources=sources)
        decimals = _sources(
            pa.array([Decimal(1)], pa.decimal128(10, 0)),
            pa.array([Decimal(1)], pa.decimal128(10, 0)),
        )
        assert fk_out_of_core_dtype_hazard(plan, _graph(), caller_sources=decimals)

    def test_a_strategy_the_runner_cannot_type_is_a_hazard(self) -> None:
        sources = _sources(pa.array(["a"]), pa.array(["a"]))
        plan = _plan("not_a_strategy")
        assert fk_out_of_core_dtype_hazard(plan, _graph(), caller_sources=sources)

    def test_no_relationships_is_no_hazard(self) -> None:
        graph = RelationshipGraph(edges=(), ordering=())
        assert not fk_out_of_core_dtype_hazard(_plan("passthrough"), graph, caller_sources={})


class TestChainIntermediateKeyIsFlagged:
    """A key that is both a child key (upstream edge) and a parent key (downstream
    edge) is rewritten to the masked parent value by the upstream join, so its
    runtime type is not its source type. Typing it from the source schema misses an
    out-of-core reject the oracle completes (the Codex final-gate finding): a hashed
    int parent key makes the intermediate key a string, and the downstream join then
    rejects its string/int candidates. Flag any such edge conservatively.
    """

    @staticmethod
    def _chain_plan() -> Any:
        return SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=b"\x01" * 8,
                per_table=(
                    ("parent", TableSeed(per_column=(("id", _seed("hash")),), per_group=())),
                    (
                        "child",
                        TableSeed(per_column=(("parent_id", _seed("passthrough")),), per_group=()),
                    ),
                    ("grand", TableSeed(per_column=(("ref", _seed("passthrough")),), per_group=())),
                ),
            )
        )

    @staticmethod
    def _chain_graph() -> RelationshipGraph:
        upstream = RelationshipEdge(
            parent_table="parent",
            parent_columns=("id",),
            child_table="child",
            child_columns=("parent_id",),
            namespace="ns",
            orphan_policy=OrphanPolicy.FAIL,
        )
        downstream = RelationshipEdge(
            parent_table="child",
            parent_columns=("parent_id",),
            child_table="grand",
            child_columns=("ref",),
            namespace="ns",
            orphan_policy=OrphanPolicy.PRESERVE,
        )
        return RelationshipGraph(edges=(upstream, downstream), ordering=())

    def test_chain_intermediate_key_is_flagged(self) -> None:
        # All int keys, parent hashed: the oracle completes, but out-of-core rejects
        # the string/int middle key. Pre-fix this returned False (a new failing job).
        sources = {
            "parent": pa.table({"id": pa.array([1, 2], pa.int64())}),
            "child": pa.table({"parent_id": pa.array([1, 2], pa.int64())}),
            "grand": pa.table({"ref": pa.array([1, 2], pa.int64())}),
        }
        assert (
            fk_out_of_core_dtype_hazard(
                self._chain_plan(), self._chain_graph(), caller_sources=sources
            )
            is True
        )

    def test_a_two_child_star_is_not_over_flagged(self) -> None:
        # Two children of one parent, distinct key columns: no key is both a parent
        # and a child key, so the chain guard must not fire. Plain int keys clear.
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=b"\x01" * 8,
                per_table=(
                    ("parent", TableSeed(per_column=(("pk", _seed("passthrough")),), per_group=())),
                ),
            )
        )
        edge_a = RelationshipEdge(
            parent_table="parent",
            parent_columns=("pk",),
            child_table="child_a",
            child_columns=("fk_a",),
            namespace="ns",
            orphan_policy=OrphanPolicy.PRESERVE,
        )
        edge_b = RelationshipEdge(
            parent_table="parent",
            parent_columns=("pk",),
            child_table="child_b",
            child_columns=("fk_b",),
            namespace="ns",
            orphan_policy=OrphanPolicy.PRESERVE,
        )
        graph = RelationshipGraph(edges=(edge_a, edge_b), ordering=())
        sources = {
            "parent": pa.table({"pk": pa.array([1, 2], pa.int64())}),
            "child_a": pa.table({"fk_a": pa.array([1, 2], pa.int64())}),
            "child_b": pa.table({"fk_b": pa.array([1, 2], pa.int64())}),
        }
        assert fk_out_of_core_dtype_hazard(plan, graph, caller_sources=sources) is False


class TestLazySourcesReadTheFooter:
    def test_schema_comes_from_a_lazy_source(self, tmp_path: Path) -> None:
        def write(name: str, table: pa.Table) -> LazySource:
            path = tmp_path / f"{name}.parquet"
            pq.write_table(table, path)
            return LazySource(path=path)

        ok = {
            "parent": write("p", pa.table({"pk": pa.array([1], pa.int64())})),
            "child": write("c", pa.table({"fk": pa.array([1], pa.int64())})),
        }
        bad = {
            "parent": write("p2", pa.table({"pk": pa.array([1], pa.uint64())})),
            "child": write("c2", pa.table({"fk": pa.array([1], pa.uint64())})),
        }
        assert not fk_out_of_core_dtype_hazard(_plan("passthrough"), _graph(), caller_sources=ok)
        assert fk_out_of_core_dtype_hazard(_plan("passthrough"), _graph(), caller_sources=bad)
