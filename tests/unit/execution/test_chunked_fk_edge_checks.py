"""Unit tests for the chunked FK gate's named steps: each `EDGE_CHECKS` row on a
hand-built `FkEdge`, and the `_edges_into` walk that feeds them.

End-to-end order and message coverage lives in
`test_chunked_fk_gate_characterization.py`; these tests pin each step in
isolation, so a failure names the step that changed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from decoy_engine.execution._chunked_fk import _edges_into
from decoy_engine.execution._chunked_fk_edge_checks import EDGE_CHECKS, EdgeCheck, FkEdge

Column = dict[str, Any]

# Passed as a `_column` override to leave that key out of the column entry.
_DROP = object()


def _column(**overrides: Any) -> Column:
    col: Column = {"strategy": "hash", "namespace": "ns", "dtype": "int64"}
    col.update(overrides)
    return {k: v for k, v in col.items() if v is not _DROP}


def _edge(
    *,
    parent: Column | None = None,
    child: Column | None = None,
    parent_cols: Any = ("id",),
    child_cols: Any = ("customer_id",),
    rel_ns: str | None = None,
    orphan_policy: Any = "remap",
    child_endpoint_columns: frozenset[tuple[str, str]] = frozenset(),
) -> FkEdge:
    col_index = {
        ("customers", "id"): _column() if parent is None else parent,
        ("orders", "customer_id"): _column() if child is None else child,
    }
    return FkEdge(
        parent_table="customers",
        parent_cols=list(parent_cols),
        child_table="orders",
        child_cols=list(child_cols),
        rel_ns=rel_ns,
        orphan_policy=orphan_policy,
        col_index=col_index,
        child_endpoint_columns=child_endpoint_columns,
    )


def _check(name: str) -> EdgeCheck:
    return next(c for c in EDGE_CHECKS if c.name == name)


def test_checks_run_in_the_documented_order() -> None:
    assert [c.name for c in EDGE_CHECKS] == [
        "orphan_policy_not_remap",
        "composite_unsupported",
        "parent_strategy_not_self_mask_safe",
        "parent_namespace_missing",
        "parent_namespace_mismatch",
        "child_namespace_missing",
        "child_namespace_mismatch",
        "child_strategy_missing",
        "child_strategy_mismatch",
        "parent_not_root",
        "parent_when_unsupported",
        "child_when_unsupported",
        "parent_provider_not_scalar",
        "child_provider_not_scalar",
        "child_config_mismatch",
        "child_key_dtype_unprovable",
        "child_key_dtype_mismatch",
        "parent_key_dtype_not_cross_adapter_safe",
        "child_key_dtype_not_cross_adapter_safe",
    ]


def test_an_admissible_edge_trips_no_check() -> None:
    edge = _edge()
    assert [c.name for c in EDGE_CHECKS if c.rejects(edge)] == []


# (check name, edge that only that check rejects, expected code, expected path)
_STEP_CASES: list[tuple[str, Callable[[], FkEdge], str, str]] = [
    (
        "orphan_policy_not_remap",
        lambda: _edge(orphan_policy="fail"),
        "chunked_fk_orphan_policy_not_remap",
        "relationships[customers.['id']->orders.['customer_id']].orphan_policy",
    ),
    (
        "composite_unsupported",
        lambda: _edge(parent_cols=("id", "region")),
        "chunked_fk_composite_unsupported",
        "relationships[customers.['id', 'region']->orders.['customer_id']].columns",
    ),
    (
        "parent_strategy_not_self_mask_safe",
        lambda: _edge(parent=_column(strategy="truncate")),
        "chunked_fk_parent_strategy_not_self_mask_safe",
        "tables.customers.columns.id.strategy",
    ),
    (
        "parent_namespace_missing",
        lambda: _edge(parent=_column(namespace=_DROP)),
        "chunked_fk_parent_namespace_missing",
        "tables.customers.columns.id.namespace",
    ),
    (
        "parent_namespace_mismatch",
        lambda: _edge(rel_ns="other"),
        "chunked_fk_parent_namespace_mismatch",
        "relationships[customers.['id']->orders.['customer_id']].namespace",
    ),
    (
        "child_namespace_missing",
        lambda: _edge(child=_column(namespace=_DROP)),
        "chunked_fk_child_namespace_missing",
        "tables.orders.columns.customer_id.namespace",
    ),
    (
        "child_namespace_mismatch",
        lambda: _edge(child=_column(namespace="other")),
        "chunked_fk_child_namespace_mismatch",
        "tables.orders.columns.customer_id.namespace",
    ),
    (
        "child_strategy_missing",
        lambda: _edge(child=_column(strategy=_DROP)),
        "chunked_fk_child_strategy_missing",
        "tables.orders.columns.customer_id.strategy",
    ),
    (
        "child_strategy_mismatch",
        lambda: _edge(child=_column(strategy="fpe")),
        "chunked_fk_child_strategy_mismatch",
        "tables.orders.columns.customer_id.strategy",
    ),
    (
        "parent_not_root",
        lambda: _edge(child_endpoint_columns=frozenset({("customers", "id")})),
        "chunked_fk_parent_not_root",
        "tables.customers.columns.id",
    ),
    (
        "parent_when_unsupported",
        lambda: _edge(parent=_column(when="id > 0")),
        "chunked_fk_parent_when_unsupported",
        "tables.customers.columns.id.when",
    ),
    (
        "child_when_unsupported",
        lambda: _edge(child=_column(when="customer_id > 0")),
        "chunked_fk_child_when_unsupported",
        "tables.orders.columns.customer_id.when",
    ),
    (
        "parent_provider_not_scalar",
        lambda: _edge(parent=_column(provider="bundle")),
        "chunked_fk_endpoint_not_scalar",
        "tables.customers.columns.id.provider",
    ),
    (
        "child_provider_not_scalar",
        lambda: _edge(child=_column(provider="bundle")),
        "chunked_fk_endpoint_not_scalar",
        "tables.orders.columns.customer_id.provider",
    ),
    (
        "child_config_mismatch",
        lambda: _edge(parent=_column(provider_config={"length": 8})),
        "chunked_fk_child_config_mismatch",
        "tables.orders.columns.customer_id.provider_config",
    ),
    (
        "child_key_dtype_unprovable",
        lambda: _edge(parent=_column(dtype=_DROP)),
        "chunked_fk_child_key_dtype_unprovable",
        "tables.orders.columns.customer_id.dtype",
    ),
    (
        "child_key_dtype_mismatch",
        lambda: _edge(child=_column(dtype="string")),
        "chunked_fk_child_key_dtype_mismatch",
        "tables.orders.columns.customer_id.dtype",
    ),
    (
        "parent_key_dtype_not_cross_adapter_safe",
        lambda: _edge(
            parent=_column(dtype="decimal256(40, 2)"), child=_column(dtype="decimal128(10, 2)")
        ),
        "chunked_fk_key_dtype_not_cross_adapter_safe",
        "tables.customers.columns.id.dtype",
    ),
    (
        "child_key_dtype_not_cross_adapter_safe",
        lambda: _edge(child=_column(dtype="int64[pyarrow]")),
        "chunked_fk_key_dtype_not_cross_adapter_safe",
        "tables.orders.columns.customer_id.dtype",
    ),
]


def test_step_cases_cover_every_check() -> None:
    assert [name for name, *_ in _STEP_CASES] == [c.name for c in EDGE_CHECKS]


@pytest.mark.parametrize(
    "name, build, code, path", _STEP_CASES, ids=[name for name, *_ in _STEP_CASES]
)
def test_each_check_is_the_first_to_reject_its_edge(
    name: str, build: Callable[[], FkEdge], code: str, path: str
) -> None:
    edge = build()
    first = next(c for c in EDGE_CHECKS if c.rejects(edge))
    assert first.name == name
    err = first.error(edge)
    assert (err.code, err.path) == (code, path)
    # The message names the edge it is about.
    assert "customers" in err.message
    assert "orders" in err.message


@pytest.mark.parametrize("when", ["", "   ", None, 7])
def test_when_checks_ignore_blank_and_non_string_values(when: object) -> None:
    edge = _edge(parent=_column(when=when), child=_column(when=when))
    assert not _check("parent_when_unsupported").rejects(edge)
    assert not _check("child_when_unsupported").rejects(edge)


def test_provider_config_none_and_empty_are_the_same() -> None:
    edge = _edge(parent=_column(provider_config=None), child=_column(provider_config={}))
    assert not _check("child_config_mismatch").rejects(edge)


def test_edge_namespace_equal_to_parent_is_admitted() -> None:
    assert not _check("parent_namespace_mismatch").rejects(_edge(rel_ns="ns"))


def test_dtype_checks_are_skipped_for_dtype_invariant_strategies() -> None:
    # Unreachable through the gate today (only hash passes the strategy check),
    # but the dtype steps still honour the dtype-invariant carve-out.
    edge = _edge(parent=_column(strategy="redact", dtype=_DROP), child=_column(strategy="redact"))
    for name in (
        "child_key_dtype_unprovable",
        "child_key_dtype_mismatch",
        "parent_key_dtype_not_cross_adapter_safe",
        "child_key_dtype_not_cross_adapter_safe",
    ):
        assert not _check(name).rejects(edge), name


def _relationship(parent: str, child: str, **extra: Any) -> dict[str, Any]:
    return {
        "parent": {"table": parent, "columns": ["id"]},
        "children": [{"table": child, "columns": [f"{parent}_id"]}],
        **extra,
    }


def test_edges_into_yields_only_edges_into_the_table_in_config_order() -> None:
    config = {
        "relationships": [
            _relationship("customers", "orders", namespace="a", orphan_policy="remap"),
            "junk",
            _relationship("customers", "invoices"),
            _relationship("stores", "orders", orphan_policy="warn"),
        ]
    }
    edges = list(_edges_into(config, "orders", {}, set()))
    assert [(e.parent_table, e.child_cols, e.rel_ns, e.orphan_policy) for e in edges] == [
        ("customers", ["customers_id"], "a", "remap"),
        ("stores", ["stores_id"], None, "warn"),
    ]


def test_edges_into_is_lazy() -> None:
    # A malformed later relationship is only read after earlier edges are gated.
    config = {"relationships": [_relationship("customers", "orders"), {"parent": ["bad"]}]}
    walk = _edges_into(config, "orders", {}, set())
    assert next(walk).parent_table == "customers"
    with pytest.raises(AttributeError):
        next(walk)
