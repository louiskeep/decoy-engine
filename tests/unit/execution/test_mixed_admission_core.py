"""F2: the shared mixed-admission core and the pre-resolved column carrier.

The core is used by the production mixed route and the shadow coordinator, so
these tests pin its rules directly (the shadow's own suite pins the same rules
through the coordinator), and pin that its codes cannot drift from the shadow
catalog. The carrier tests pin the key/payload alignment contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import _chunked_input
from decoy_engine.execution import _mixed_admission as core
from decoy_engine.execution._chunked_preresolved import PreResolvedColumn
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.physical import _shadow_diff_codes as shadow_codes


def _edge(
    parent: str = "gen",
    child: str = "mask",
    parent_cols: tuple[str, ...] = ("id",),
    child_cols: tuple[str, ...] = ("fk",),
) -> Any:
    return SimpleNamespace(
        parent_table=parent,
        child_table=child,
        parent_columns=parent_cols,
        child_columns=child_cols,
    )


def _graph(*edges: Any) -> Any:
    return SimpleNamespace(edges=tuple(edges))


_INT64 = pa.int64()


def _select(graph: Any, child_type: pa.DataType | None = _INT64) -> Any:
    return core.select_admitted_coupled_edge(
        graph, frozenset({"gen"}), frozenset({"mask", "mask2"}), lambda t, c: child_type
    )


def test_core_codes_equal_the_shadow_catalog() -> None:
    assert core.GENERATION_SHAPE_UNSUPPORTED == shadow_codes.GENERATION_SHAPE_UNSUPPORTED
    assert core.MIXED_DRIVER_UNSUPPORTED == shadow_codes.MIXED_DRIVER_UNSUPPORTED
    assert (
        core.MIXED_FK_CROSS_GENERATE_UNSUPPORTED == shadow_codes.MIXED_FK_CROSS_GENERATE_UNSUPPORTED
    )
    assert core.MIXED_FK_TOPOLOGY_UNSUPPORTED == shadow_codes.MIXED_FK_TOPOLOGY_UNSUPPORTED
    for code in (
        core.GENERATION_SHAPE_UNSUPPORTED,
        core.MIXED_DRIVER_UNSUPPORTED,
        core.MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
        core.MIXED_FK_TOPOLOGY_UNSUPPORTED,
    ):
        assert code in shadow_codes.DIFFERENCE_CODES


def test_no_crossing_edge_is_the_independent_case() -> None:
    assert _select(_graph()) is None
    assert _select(_graph(_edge(parent="gen", child="gen2"))) is None


def test_one_crossing_edge_is_admitted() -> None:
    edge = _edge()
    assert _select(_graph(edge)) is edge


def test_two_crossing_edges_decline() -> None:
    with pytest.raises(core.MixedDeclineError) as excinfo:
        _select(_graph(_edge(), _edge(child="mask2", child_cols=("fk2",))))
    assert excinfo.value.code == core.MIXED_FK_TOPOLOGY_UNSUPPORTED


@pytest.mark.parametrize(
    "edge",
    [_edge(parent_cols=("a", "b"), child_cols=("x", "y")), _edge(child_cols=("x", "y"))],
    ids=["both_composite", "child_composite"],
)
def test_a_composite_key_declines(edge: Any) -> None:
    with pytest.raises(core.MixedDeclineError) as excinfo:
        _select(_graph(edge))
    assert excinfo.value.code == core.MIXED_FK_CROSS_GENERATE_UNSUPPORTED


@pytest.mark.parametrize("key_type", [pa.float64(), pa.bool_(), pa.timestamp("us"), None])
def test_a_non_int_non_string_child_key_declines(key_type: pa.DataType | None) -> None:
    with pytest.raises(core.MixedDeclineError) as excinfo:
        _select(_graph(_edge()), key_type)
    assert excinfo.value.code == core.MIXED_FK_CROSS_GENERATE_UNSUPPORTED


@pytest.mark.parametrize("key_type", [pa.int8(), pa.uint64(), pa.string(), pa.large_string()])
def test_int_and_string_child_keys_are_admitted(key_type: pa.DataType) -> None:
    assert _select(_graph(_edge()), key_type) is not None


def test_another_edge_touching_a_mask_table_declines() -> None:
    other = _edge(parent="mask", child="mask2", parent_cols=("a",), child_cols=("b",))
    with pytest.raises(core.MixedDeclineError) as excinfo:
        _select(_graph(_edge(), other))
    assert excinfo.value.code == core.MIXED_FK_TOPOLOGY_UNSUPPORTED


def test_a_generate_to_generate_edge_alongside_the_coupled_edge_is_admitted() -> None:
    edge = _edge()
    assert _select(_graph(edge, _edge(parent="gen", child="gen2"))) is edge


def test_an_out_of_core_driver_always_declines() -> None:
    core.require_no_out_of_core_driver(out_of_core_present=False)
    with pytest.raises(core.MixedDeclineError) as excinfo:
        core.require_no_out_of_core_driver(out_of_core_present=True)
    assert excinfo.value.code == core.MIXED_DRIVER_UNSUPPORTED


def test_parent_key_type_is_checked_against_the_generated_output() -> None:
    edge = _edge()
    core.require_admitted_parent_key_type(edge, {"gen": pa.table({"id": [1, 2]})})
    core.require_admitted_parent_key_type(edge, {"gen": pa.table({"id": ["a"]})})
    for outputs in ({"gen": pa.table({"id": [1.5]})}, {"gen": pa.table({"other": [1]})}, {}):
        with pytest.raises(core.MixedDeclineError) as excinfo:
            core.require_admitted_parent_key_type(edge, outputs)
        assert excinfo.value.code == core.MIXED_FK_CROSS_GENERATE_UNSUPPORTED


@pytest.mark.parametrize(
    "column",
    [
        pa.array([1, None], pa.int64()),
        pa.array([1.0, float("nan")], pa.float64()),
        pa.array([[1], [2]], pa.list_(pa.int64())),
        pa.array([{"a": 1}], pa.struct([("a", pa.int64())])),
    ],
    ids=["null", "nan", "list", "struct"],
)
def test_unstable_generate_output_declines(column: pa.Array) -> None:
    with pytest.raises(core.MixedDeclineError) as excinfo:
        core.require_roundtrip_stable_generate_outputs({"gen": pa.table({"c": column})})
    assert excinfo.value.code == core.GENERATION_SHAPE_UNSUPPORTED


def test_stable_generate_output_is_admitted() -> None:
    core.require_roundtrip_stable_generate_outputs(
        {"gen": pa.table({"i": [1, 2], "f": [0.5, 1.5], "s": ["a", "b"]})}
    )


# ---------------------------------------------------------------------------
# The pre-resolved column carrier: key/payload alignment contract.
# ---------------------------------------------------------------------------

SRC = pa.array(["a", "b", None, "c", "d", "e", "f"], pa.string())
RESOLVED = pa.array(["A", "B", None, "C", "D", "E", "F"], pa.string())


def _table(keys: pa.Array, extra: list[int] | None = None) -> pa.Table:
    return pa.table({"fk": keys, "v": extra if extra is not None else list(range(len(keys)))})


def _carrier(resolved: pa.Array = RESOLVED, source: pa.Array = SRC) -> PreResolvedColumn:
    return PreResolvedColumn("t", "fk", source, resolved)


def _chunks(sizes: list[int]) -> list[pa.Table]:
    whole, out, start = _table(SRC), [], 0
    for size in sizes:
        out.append(whole.slice(start, size))
        start += size
    return out


def test_uneven_chunks_receive_their_own_slice_in_order() -> None:
    out = list(_carrier().wrap(_chunks([1, 4, 2])))
    assert [c.num_rows for c in out] == [1, 4, 2]
    assert pa.concat_tables(out).column("fk").to_pylist() == RESOLVED.to_pylist()
    assert pa.concat_tables(out).column("v").to_pylist() == list(range(7))


def test_a_source_key_that_differs_from_the_resolved_rows_raises() -> None:
    chunks = _chunks([3, 4])
    chunks[1] = chunks[1].set_column(0, "fk", pa.array(["X", "d", "e", "f"], pa.string()))
    with pytest.raises(ExecutionError) as excinfo:
        list(_carrier().wrap(chunks))
    assert excinfo.value.code == "mixed_key_payload_misaligned"


def test_a_reordered_payload_stream_raises() -> None:
    with pytest.raises(ExecutionError) as excinfo:
        list(_carrier().wrap(reversed(_chunks([3, 4]))))
    assert excinfo.value.code == "mixed_key_payload_misaligned"


def test_a_short_payload_stream_raises_at_the_end() -> None:
    with pytest.raises(ExecutionError) as excinfo:
        list(_carrier().wrap(_chunks([3, 3])))
    assert excinfo.value.code == "mixed_key_payload_misaligned"
    assert "ended at 6 rows but 7" in str(excinfo.value)


def test_a_long_payload_stream_raises() -> None:
    extra = [*_chunks([7]), _table(pa.array(["z"], pa.string()))]
    with pytest.raises(ExecutionError) as excinfo:
        list(_carrier().wrap(extra))
    assert excinfo.value.code == "mixed_key_payload_misaligned"


def test_source_and_resolved_of_different_length_raise_at_construction() -> None:
    with pytest.raises(ExecutionError):
        PreResolvedColumn("t", "fk", SRC, RESOLVED.slice(1))


def test_the_resolved_type_replaces_the_field_type_and_keeps_its_flags() -> None:
    schema = pa.schema(
        [
            pa.field("fk", pa.large_string(), nullable=False, metadata={"k": "v"}),
            pa.field("v", pa.int64()),
        ]
    )
    out = _carrier().schema_for(schema)
    assert out.field("fk").type == pa.string()
    assert out.field("fk").nullable is False and out.field("fk").metadata == {b"k": b"v"}
    assert _carrier().schema_for(pa.schema([("fk", pa.string())])).field("fk").type == pa.string()


def test_a_fixed_schema_producer_stays_a_fixed_schema_producer() -> None:
    producer = _chunked_input.fixed_schema_chunks_from_resident(_table(SRC), 3)
    wrapped = _carrier().wrap(producer)
    assert isinstance(wrapped, _chunked_input._FixedSchemaChunks)
    assert wrapped.source_schema.equals(producer.source_schema, check_metadata=True)
    assert [c.num_rows for c in wrapped] == [3, 3, 1]
    assert all(c.schema.equals(wrapped.source_schema, check_metadata=True) for c in wrapped)
    # re-iterable, like the resident producer it wraps
    assert pa.concat_tables(list(wrapped)).column("fk").to_pylist() == RESOLVED.to_pylist()


def test_an_int_key_above_two_to_the_53_is_carried_exactly() -> None:
    big = 2**53 + 12345
    source = pa.array([big, big + 1, 7], pa.int64())
    resolved = pa.array([big, big + 1, 7], pa.int64())
    out = list(PreResolvedColumn("t", "fk", source, resolved).wrap([_table(source)]))
    assert out[0].column("fk").to_pylist() == [big, big + 1, 7]


# ---------------------------------------------------------------------------
# The routing decision: the mixed branch is reachable only under `auto`.
# ---------------------------------------------------------------------------


def _decide(mode: str, *, mixed_admitted: bool) -> tuple[str, str]:
    from decoy_engine.execution._pipeline_routing import decide_execution_route
    from decoy_engine.relationships import RelationshipGraph

    return decide_execution_route(
        SimpleNamespace(relationships=()),
        has_generate_table=True,
        has_mask_table=True,
        validators=[],
        fidelity_report=False,
        vault_writer=None,
        execution_mode=mode,
        graph=RelationshipGraph(edges=(), ordering=()),
        mixed_admitted=mixed_admitted,
    )


def test_an_admitted_mixed_job_routes_mixed_under_auto_only() -> None:
    assert _decide("auto", mixed_admitted=True) == ("mixed", "mixed_generate_mask_chunked")
    assert _decide("auto", mixed_admitted=False)[0] == "full_frame"
    assert _decide("full_frame", mixed_admitted=True) == ("full_frame", "override_full_frame")


@pytest.mark.parametrize("mode", ["sequential", "out_of_core"])
def test_forced_bounded_modes_still_reject_even_when_the_mixed_shape_is_admitted(mode: str) -> None:
    from decoy_engine.errors import ConfigError

    with pytest.raises(ConfigError):
        _decide(mode, mixed_admitted=True)
