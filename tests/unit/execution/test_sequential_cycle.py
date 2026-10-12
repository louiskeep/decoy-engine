"""Phase D / D2-cycle: the component detector, table order, admission gate and memory pricing.

Pure-Python checks over hand-built plans and graphs. Run-time behavior (parity with the
full-frame oracle, the two passes, map release) lives in
`tests/parity/test_phase_d_d2cycle_seq.py`.
"""

from __future__ import annotations

import dataclasses

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._sequential import table_topo_order
from decoy_engine.execution._sequential_cycle import fk_components, sequential_table_order
from decoy_engine.execution._sequential_cycle_admit import (
    BUDGET_EXCEEDED,
    BUDGET_UNAVAILABLE,
    KEY_STRATEGY,
    KEY_WHEN,
    KEY_WRITER,
    PAYLOAD_OVER_BUDGET,
    PAYLOAD_UNPRICED,
    PROJECTED_READ,
    QUARANTINE,
    TRANSFORMS,
    WORK_GRAPH_CYCLIC,
    CycleContext,
    cycle_decline,
    price_cycle_job,
    require_cycle_admitted,
)
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d2cycle_shapes import (
    REGISTRY,
    chain_cycle_shape,
    cycle_ctx,
    mixed_topology_shape,
    three_table_cycle_shape,
    two_table_cycle_shape,
)

PRESERVE = OrphanPolicy.PRESERVE
_QUARANTINE = {"enabled": True, "triggers": ["mask_error"], "output_path": "q.jsonl"}
_POOL = 256 * 1024 * 1024


def _decline(shape: s.Shape, ctx: CycleContext | None = None, **kwargs: object) -> object:
    return cycle_decline(
        shape.plan,
        shape.graph,
        REGISTRY,
        ctx if ctx is not None else cycle_ctx(shape),
        pool_capacity_bytes=kwargs.pop("pool_capacity_bytes", _POOL),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _code(shape: s.Shape, ctx: CycleContext | None = None, **kwargs: object) -> str | None:
    found = _decline(shape, ctx, **kwargs)
    return None if found is None else found.code  # type: ignore[attr-defined]


# Component detector.


def _names(components: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [tuple(sorted(c)) for c in components]


def test_two_three_and_chain_cycles_are_one_component_each() -> None:
    assert _names(fk_components(two_table_cycle_shape().graph)) == [("A", "B")]
    assert _names(fk_components(three_table_cycle_shape().graph)) == [("A", "B", "C")]
    assert _names(fk_components(chain_cycle_shape().graph)) == [("A", "B", "C")]


def test_mixed_topology_component_excludes_the_acyclic_neighbours() -> None:
    assert _names(fk_components(mixed_topology_shape().graph)) == [("A", "B")]


def test_self_reference_and_acyclic_graphs_have_no_component() -> None:
    assert fk_components(s.self_ref_shape().graph) == []
    for shape in (s.chain_shape(), s.diamond_shape(), s.junction_shape(), s.multi_parent_shape()):
        assert fk_components(shape.graph) == []


def test_two_disjoint_cycles_are_two_components() -> None:
    g = s.graph(
        s.edge("A", "k", "B", "k", "n1"),
        s.edge("B", "j", "A", "j", "n2"),
        s.edge("X", "k", "Y", "k", "n3"),
        s.edge("Y", "j", "X", "j", "n4"),
    )
    assert sorted(_names(fk_components(g))) == [("A", "B"), ("X", "Y")]


# Table order.


@pytest.mark.parametrize(
    "build",
    [s.chain_shape, s.diamond_shape, s.junction_shape, s.multi_parent_shape, s.self_ref_shape],
)
def test_acyclic_order_is_exactly_the_existing_topological_order(build) -> None:  # type: ignore[no-untyped-def]
    shape = build()
    order, components = sequential_table_order(shape.plan, shape.graph)
    assert order == table_topo_order(shape.plan, shape.graph)
    assert components == []


def test_mixed_topology_orders_parents_first_and_keeps_every_table_once() -> None:
    shape = mixed_topology_shape()
    order, components = sequential_table_order(shape.plan, shape.graph)
    assert sorted(order) == ["A", "B", "C", "D", "P"]
    assert order.index("P") < min(order.index("A"), order.index("B"))
    assert order.index("C") > max(order.index("A"), order.index("B"))
    assert _names(components) == [("A", "B")]


def test_cyclic_component_tables_are_contiguous_in_plan_order() -> None:
    order, _ = sequential_table_order(
        three_table_cycle_shape().plan, three_table_cycle_shape().graph
    )
    assert order == ["A", "B", "C"]


# Admission: admitted shapes.


@pytest.mark.parametrize(
    "build",
    [two_table_cycle_shape, three_table_cycle_shape, chain_cycle_shape, mixed_topology_shape],
)
def test_the_admitted_producers_are_admitted(build) -> None:  # type: ignore[no-untyped-def]
    assert _code(build()) is None


@pytest.mark.parametrize(
    "seed",
    [
        column_seed("hash", namespace="ns_k"),
        column_seed("passthrough", namespace=None),
        column_seed("redact", namespace=None),
        column_seed("truncate", namespace=None, provider_config=(("length", 3),)),
        column_seed("fpe", namespace="ns_k", provider_config=(("charset", "digits"),)),
    ],
    ids=["hash", "passthrough", "redact", "truncate", "fpe"],
)
def test_each_allowlisted_strategy_is_an_admitted_producer(seed: ColumnSeed) -> None:
    assert _code(two_table_cycle_shape(id_seed=seed)) is None


def test_acyclic_graph_is_never_declined() -> None:
    shape = s.chain_shape()
    assert _decline(shape, cycle_ctx(shape, budget=None)) is None


# Admission: declines (default-deny producers).


@pytest.mark.parametrize(
    "strategy",
    ["faker", "categorical", "shuffle", "derived", "date_shift", "bucketize", "nested", "formula"],
)
def test_every_other_strategy_on_a_cyclic_key_is_declined(strategy: str) -> None:
    seed = column_seed(strategy, namespace="ns_k")
    assert _code(two_table_cycle_shape(id_seed=seed)) == KEY_STRATEGY


def test_derived_key_reading_a_payload_sibling_is_declined() -> None:
    """The rev-3 counterexample: a key-only read cannot evaluate `pay + 100`."""
    seed = column_seed("derived", namespace=None, provider_config=(("expression", "pay + 100"),))
    assert _code(two_table_cycle_shape(id_seed=seed)) == KEY_STRATEGY


def test_positional_key_with_duplicate_source_keys_is_declined() -> None:
    """A last-wins map would drop distinct per-row outputs of a positional strategy."""
    seed = column_seed("shuffle", namespace="ns_k")
    assert _code(two_table_cycle_shape(id_seed=seed, a_id=["a1", "a1", "a2", "a2"])) == KEY_STRATEGY


def test_an_allowlisted_strategy_that_reads_a_sibling_is_declined() -> None:
    seed = dataclasses.replace(column_seed("hash", namespace="ns_k"), coherent_with=("pay",))
    assert _code(two_table_cycle_shape(id_seed=seed)) == KEY_STRATEGY


def test_a_when_gated_cyclic_key_is_declined() -> None:
    seed = dataclasses.replace(column_seed("hash", namespace="ns_k"), when="pay != 'x'")
    assert _code(two_table_cycle_shape(id_seed=seed)) == KEY_WHEN


def test_a_when_gated_incoming_parent_outside_the_cycle_is_admitted() -> None:
    """The gate on P.k leaves some rows raw in P's map, which the cycle inherits as is."""
    assert _code(mixed_topology_shape(p_key_when="gate == 'on'")) is None


def test_a_composite_provider_on_a_cyclic_key_is_declined() -> None:
    seed = dataclasses.replace(
        column_seed("hash", namespace="ns_k"), provider="composite_person", coherent_with=("pay",)
    )
    assert _code(two_table_cycle_shape(id_seed=seed)) in (KEY_STRATEGY, KEY_WRITER)


def _joint_mask(columns: tuple[str, ...]) -> ColumnSeed:
    return column_seed(
        "joint_mask",
        namespace="ns_jm",
        provider_config=(("columns", columns), ("reference", "zips"), ("key_by", "pay")),
    )


def test_payload_joint_mask_that_lists_a_cyclic_key_is_declined() -> None:
    """The rev-4 counterexample: the key is nominally hash, but joint_mask can overwrite it."""
    shape = two_table_cycle_shape(
        extra_a={"zip": (_joint_mask(("id", "zip")), pa.array(["z1", "z2", "z3", "z4"]))}
    )
    assert _code(shape) == KEY_WRITER


def test_payload_joint_mask_that_leaves_the_keys_alone_is_admitted() -> None:
    shape = two_table_cycle_shape(
        extra_a={"zip": (_joint_mask(("zip", "city")), pa.array(["z1", "z2", "z3", "z4"]))}
    )
    assert _code(shape) is None


def test_an_entry_with_an_undeclared_strategy_blocks_the_table() -> None:
    mystery = column_seed("hash", namespace="ns_k")
    mystery = dataclasses.replace(mystery, strategy="mystery_writer")
    shape = two_table_cycle_shape(extra_a={"other": (mystery, pa.array(list("wxyz")))})
    assert _code(shape) == KEY_WRITER


# Admission: job-level declines.


def test_table_transforms_on_a_cyclic_table_are_declined() -> None:
    shape = two_table_cycle_shape()
    assert _code(shape, cycle_ctx(shape, transforms=frozenset({"B"}))) == TRANSFORMS


def test_transforms_on_an_unrelated_acyclic_table_do_not_decline() -> None:
    shape = mixed_topology_shape()
    assert _code(shape, cycle_ctx(shape, transforms=frozenset({"D"}))) is None


def test_job_level_quarantine_triggers_decline_the_cyclic_route() -> None:
    assert _code(two_table_cycle_shape(), quarantine_config=_QUARANTINE) == QUARANTINE


def test_quarantine_triggers_decline_for_the_acyclic_tables_around_the_cycle_too() -> None:
    assert _code(mixed_topology_shape(), quarantine_config=_QUARANTINE) == QUARANTINE


@pytest.mark.parametrize(
    "config",
    [None, {}, {"enabled": False, "triggers": ["mask_error"]}, {"enabled": True, "triggers": []}],
)
def test_quarantine_that_can_cover_nothing_does_not_decline(config) -> None:  # type: ignore[no-untyped-def]
    assert _code(two_table_cycle_shape(), quarantine_config=config) is None


def test_a_work_node_cycle_is_declined() -> None:
    """Both key columns are children of each other: no work order exists."""
    plan = s.make_plan(
        {"A": s.table_seed({"k": s.hashed("n1")}), "B": s.table_seed({"k": s.hashed("n2")})}
    )
    g = s.graph(s.edge("A", "k", "B", "k", "n1"), s.edge("B", "k", "A", "k", "n2"))
    shape = s.Shape(
        plan, g, {"A": pa.table({"k": s.strs(["a"])}), "B": pa.table({"k": s.strs(["a"])})}
    )
    assert _code(shape) == WORK_GRAPH_CYCLIC


def test_a_loader_without_a_projected_read_is_declined() -> None:
    shape = two_table_cycle_shape()
    assert _code(shape, cycle_ctx(shape, projected=False)) == PROJECTED_READ


def test_an_unknown_budget_is_declined_not_assumed_to_fit() -> None:
    shape = two_table_cycle_shape()
    assert _code(shape, cycle_ctx(shape, budget=None)) == BUDGET_UNAVAILABLE


def test_a_missing_table_size_is_declined() -> None:
    shape = two_table_cycle_shape()
    ctx = dataclasses.replace(cycle_ctx(shape), table_bytes={"A": 10})
    assert _code(shape, ctx) == BUDGET_UNAVAILABLE


def test_code_set_payload_state_is_unpriceable_and_declined() -> None:
    code_set = column_seed("code_set", namespace="ns_cs")
    shape = two_table_cycle_shape(extra_a={"dx": (code_set, pa.array(list("wxyz")))})
    assert _code(shape) == PAYLOAD_UNPRICED


def test_a_composite_payload_provider_is_unpriced_and_declined() -> None:
    """Its bundle pool is sized by config and never goes through the PoolCache."""
    composite = dataclasses.replace(
        column_seed("hash", namespace="ns_c"), provider="composite_city_state_zip"
    )
    shape = two_table_cycle_shape(extra_a={"city": (composite, pa.array(list("wxyz")))})
    assert _code(shape) == PAYLOAD_UNPRICED


def test_a_large_pool_cache_is_priced_and_over_budget_is_declined() -> None:
    faker = column_seed("faker", namespace="ns_f")
    shape = two_table_cycle_shape(extra_a={"mail": (faker, pa.array(list("wxyz")))})
    tiny = cycle_ctx(shape, budget=100 * 1024 * 1024)
    assert _code(shape, tiny) == PAYLOAD_OVER_BUDGET
    assert _code(shape, cycle_ctx(shape, budget=8 * 1024**3)) is None
    assert _code(shape, tiny, pool_capacity_bytes=1024) is None


def test_every_decline_is_coded_and_carries_no_row_data() -> None:
    shape = two_table_cycle_shape(id_seed=column_seed("faker", namespace="ns_k"))
    with pytest.raises(ExecutionError) as caught:
        require_cycle_admitted(
            shape.plan, shape.graph, REGISTRY, cycle_ctx(shape), quarantine_config=None
        )
    assert caught.value.code == KEY_STRATEGY
    text = str(caught.value)
    for leaked in ("a1", "a2", "a3", "aX", "zz", "p0"):
        assert leaked not in text


# Memory pricing.


def _price(shape: s.Shape, ctx: CycleContext, pool: int = 0):  # type: ignore[no-untyped-def]
    price = price_cycle_job(shape.plan, shape.graph, REGISTRY, ctx, pool_capacity_bytes=pool)
    assert price is not None
    return price


def test_every_live_allocation_has_a_term_in_the_total() -> None:
    shape = mixed_topology_shape()
    price = _price(shape, cycle_ctx(shape), pool=777)
    assert price.parent_maps_bytes > 0
    assert price.error_cache_bytes > 0
    assert price.narrow_scratch_bytes > 0
    assert price.working_set_bytes > 0
    assert price.payload_state_bytes == 0
    assert price.total == (
        price.parent_maps_bytes
        + price.error_cache_bytes
        + price.narrow_scratch_bytes
        + price.working_set_bytes
        + price.payload_state_bytes
    )


def test_pool_state_is_priced_only_when_a_pool_strategy_is_planned() -> None:
    faker = column_seed("faker", namespace="ns_f")
    plain = two_table_cycle_shape()
    pooled = two_table_cycle_shape(extra_a={"mail": (faker, pa.array(list("wxyz")))})
    assert _price(plain, cycle_ctx(plain), pool=_POOL).payload_state_bytes == 0
    assert _price(pooled, cycle_ctx(pooled), pool=_POOL).payload_state_bytes == _POOL


def test_the_table_transition_overlap_is_priced() -> None:
    """Two frames of the largest table are live at once (the frame and its output)."""
    shape = two_table_cycle_shape()
    small = dataclasses.replace(cycle_ctx(shape), table_bytes={"A": 1_000, "B": 10})
    big = dataclasses.replace(cycle_ctx(shape), table_bytes={"A": 2_000, "B": 10})
    assert _price(shape, big).working_set_bytes - _price(shape, small).working_set_bytes == 2_000


def test_maps_scale_with_parent_rows_and_the_error_term_with_them() -> None:
    shape = mixed_topology_shape()
    few = dataclasses.replace(cycle_ctx(shape), table_rows=dict.fromkeys("PABCD", 10))
    many = dataclasses.replace(cycle_ctx(shape), table_rows=dict.fromkeys("PABCD", 1_000_000))
    assert _price(shape, many).parent_maps_bytes > 1000 * _price(shape, few).parent_maps_bytes // 2
    assert _price(shape, many).error_cache_bytes > _price(shape, few).error_cache_bytes


def test_a_job_whose_aggregate_frames_exceed_the_budget_but_whose_live_state_fits_is_admitted() -> (
    None
):
    shape = mixed_topology_shape()
    sizes = dict.fromkeys("PABCD", 1_000_000_000)
    ctx = dataclasses.replace(cycle_ctx(shape), table_bytes=sizes)
    need = _price(shape, ctx).total
    assert need < sum(sizes.values())
    fits = dataclasses.replace(ctx, budget_bytes=need)
    assert _code(shape, fits) is None
    assert _code(shape, dataclasses.replace(ctx, budget_bytes=need - 1)) == BUDGET_EXCEEDED


def test_a_single_oversized_cyclic_table_is_declined() -> None:
    shape = mixed_topology_shape()
    sizes = {"P": 10, "A": 10, "B": 5_000_000_000, "C": 10, "D": 10}
    ctx = dataclasses.replace(cycle_ctx(shape, budget=3_000_000_000), table_bytes=sizes)
    assert _code(shape, ctx) == BUDGET_EXCEEDED


def test_a_mixed_topology_over_budget_in_combination_is_declined_with_no_oversized_table() -> None:
    """Incoming parent map + cyclic maps + outgoing parent map together bust the budget."""
    shape = mixed_topology_shape()
    rows = dict.fromkeys("PABCD", 5_000_000)
    tiny_tables = dict.fromkeys("PABCD", 1_000)
    ctx = dataclasses.replace(
        cycle_ctx(shape, budget=3 * 1024**3), table_rows=rows, table_bytes=tiny_tables
    )
    assert max(tiny_tables.values()) * 2 < ctx.budget_bytes  # type: ignore[operator]
    assert _code(shape, ctx) == BUDGET_EXCEEDED
    small = dataclasses.replace(ctx, table_rows=dict.fromkeys("PABCD", 1_000))
    assert _code(shape, small) is None


def test_an_error_heavy_config_whose_cell_caches_bust_the_budget_is_declined() -> None:
    shape = mixed_topology_shape()
    ctx = dataclasses.replace(cycle_ctx(shape), table_rows=dict.fromkeys("PABCD", 2_000_000))
    price = _price(shape, ctx)
    without_errors = price.total - price.error_cache_bytes
    assert price.error_cache_bytes > 0
    tight = dataclasses.replace(ctx, budget_bytes=without_errors + price.error_cache_bytes // 2)
    assert _code(shape, tight) == BUDGET_EXCEEDED
    ok = dataclasses.replace(ctx, budget_bytes=price.total)
    assert _code(shape, ok) is None
