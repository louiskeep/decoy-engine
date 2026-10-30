"""Characterization of `gate_fk_child_edges`: every reject decision, its full
machine fields, and the order the decisions are evaluated in.

`test_chunked_fk_gate_kills.py` pins each branch's code and path one at a time.
This file adds what a restructure of the gate must also preserve:

  * the exact message text of every reject (not just the data it carries), and
  * the short-circuit ORDER: a config that breaks every predicate from step k
    onward must raise step k's reject, for every k. Breaking all later steps at
    once proves an earlier step is still evaluated first.

The expected tuples were captured from the gate before it was restructured.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from decoy_engine.execution._chunked_fk import gate_fk_child_edges
from decoy_engine.plan import PlanCompileError

Config = dict[str, Any]


def _base_config() -> Config:
    """customers.id -> orders.customer_id, admitted by every predicate."""
    return {
        "global_settings": {"seed": 7},
        "tables": [
            {"name": "regions", "columns": [{"name": "id", "strategy": "hash", "namespace": "ns"}]},
            {
                "name": "customers",
                "columns": [
                    {"name": "id", "strategy": "hash", "namespace": "ns", "dtype": "int64"}
                ],
            },
            {
                "name": "orders",
                "columns": [
                    {"name": "customer_id", "strategy": "hash", "namespace": "ns", "dtype": "int64"}
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "remap",
            }
        ],
    }


def _parent(cfg: Config) -> dict[str, Any]:
    return cfg["tables"][1]["columns"][0]


def _child(cfg: Config) -> dict[str, Any]:
    return cfg["tables"][2]["columns"][0]


def _edge(cfg: Config) -> dict[str, Any]:
    return cfg["relationships"][0]


def _make_parent_a_child(cfg: Config) -> None:
    cfg["relationships"].append(
        {
            "parent": {"table": "regions", "columns": ["id"]},
            "children": [{"table": "customers", "columns": ["id"]}],
            "orphan_policy": "remap",
        }
    )


def _set_dtypes(parent: str, child: str) -> Callable[[Config], None]:
    def apply(cfg: Config) -> None:
        _parent(cfg)["dtype"] = parent
        _child(cfg)["dtype"] = child

    return apply


# One breaker per gate decision, in the gate's evaluation order.
_BREAKERS: tuple[tuple[str, Callable[[Config], Any]], ...] = (
    ("orphan_policy_not_remap", lambda c: _edge(c).__setitem__("orphan_policy", "warn")),
    ("composite", lambda c: _edge(c)["children"][0].__setitem__("columns", ["customer_id", "x"])),
    ("parent_strategy_not_hash", lambda c: _parent(c).__setitem__("strategy", "fpe")),
    ("parent_namespace_missing", lambda c: _parent(c).pop("namespace")),
    ("parent_namespace_mismatch", lambda c: _edge(c).__setitem__("namespace", "edge_ns")),
    ("child_namespace_missing", lambda c: _child(c).pop("namespace")),
    ("child_namespace_mismatch", lambda c: _child(c).__setitem__("namespace", "ns2")),
    ("child_strategy_missing", lambda c: _child(c).pop("strategy")),
    ("child_strategy_mismatch", lambda c: _child(c).__setitem__("strategy", "fpe")),
    ("parent_not_root", _make_parent_a_child),
    ("parent_when", lambda c: _parent(c).__setitem__("when", "id > 1")),
    ("child_when", lambda c: _child(c).__setitem__("when", "customer_id > 1")),
    ("parent_provider", lambda c: _parent(c).__setitem__("provider", "p")),
    ("child_provider", lambda c: _child(c).__setitem__("provider", "p")),
    ("provider_config_mismatch", lambda c: _child(c).__setitem__("provider_config", {"k": 1})),
    ("dtype_unprovable", lambda c: _child(c).pop("dtype")),
    ("dtype_family_mismatch", lambda c: _child(c).__setitem__("dtype", "float64")),
    ("parent_dtype_not_safe", _set_dtypes("date64", "date64")),
    ("child_dtype_not_safe", _set_dtypes("int64", "int64[pyarrow]")),
)


def _config_breaking_from(k: int) -> Config:
    """Apply breakers k..n, later ones first, so breaker k has the last word on
    any field two breakers share and is the earliest predicate violated."""
    cfg = copy.deepcopy(_base_config())
    for _, breaker in reversed(_BREAKERS[k:]):
        breaker(cfg)
    return cfg


def _reject(cfg: Config, table: str = "orders") -> PlanCompileError:
    with pytest.raises(PlanCompileError) as exc:
        gate_fk_child_edges(cfg, table=table)
    return exc.value


_EXPECTED: dict[str, tuple[str, str, str]] = {
    "orphan_policy_not_remap": (
        "chunked_fk_orphan_policy_not_remap",
        "relationships[customers.['id']->orders.['customer_id', 'x']].orphan_policy",
        "FK edge customers->orders: chunked self-masking requires orphan_policy 'remap'; got 'warn'. REMAP mints via the same parent strategy and namespace as self-masking. WARN/FAIL/PRESERVE require the parent key set resident. Use run_pipeline or run_sequential instead.",
    ),
    "composite": (
        "chunked_fk_composite_unsupported",
        "relationships[customers.['id']->orders.['customer_id', 'x']].columns",
        "FK edge customers->orders: composite FK edges (multi-column keys) are not supported in chunked self-masking. Use run_pipeline or run_sequential instead.",
    ),
    "parent_strategy_not_hash": (
        "chunked_fk_parent_strategy_not_self_mask_safe",
        "tables.customers.columns.id.strategy",
        "FK edge customers.id->orders.customer_id: parent key strategy 'fpe' is not self-mask safe. Chunked FK self-masking admits exactly one strategy: 'hash' (proven cross-adapter byte-identical for the exact dtype set predicate 12 restricts FK keys to). Use run_pipeline or run_sequential for any other strategy.",
    ),
    "parent_namespace_missing": (
        "chunked_fk_parent_namespace_missing",
        "tables.customers.columns.id.namespace",
        "FK edge customers.id->orders.customer_id: parent column has no namespace and strategy 'hash' requires one. The 'hash' strategy raises an error at execution when no namespace is declared. Add 'namespace: <ns>' to the parent column.",
    ),
    "parent_namespace_mismatch": (
        "chunked_fk_parent_namespace_mismatch",
        "relationships[customers.['id']->orders.['customer_id']].namespace",
        "FK edge customers.id->orders.customer_id: edge namespace 'edge_ns' disagrees with parent column namespace 'ns'. The parent column namespace is the authoritative masking key; the edge namespace is a label only and is never used to derive a masked value. Update the edge namespace to 'ns' or remove it.",
    ),
    "child_namespace_missing": (
        "chunked_fk_child_namespace_missing",
        "tables.orders.columns.customer_id.namespace",
        "FK edge customers.id->orders.customer_id: child column orders.customer_id has no explicit namespace. ColumnSeed.namespace comes from the config column entry, not from FK auto-binding. Add 'namespace: ns' to the child column.",
    ),
    "child_namespace_mismatch": (
        "chunked_fk_child_namespace_mismatch",
        "tables.orders.columns.customer_id.namespace",
        "FK edge customers.id->orders.customer_id: child namespace 'ns2' does not match parent column namespace 'ns'. Self-masking requires child_ns == parent_ns so derive(seed, ns, value) produces byte-identical output.",
    ),
    "child_strategy_missing": (
        "chunked_fk_child_strategy_missing",
        "tables.orders.columns.customer_id.strategy",
        "FK edge customers.id->orders.customer_id: child column orders.customer_id has no explicit strategy. The by-reference model would stream raw FK key values without the parent map resident. Add 'strategy: hash' to the child column.",
    ),
    "child_strategy_mismatch": (
        "chunked_fk_child_strategy_mismatch",
        "tables.orders.columns.customer_id.strategy",
        "FK edge customers.id->orders.customer_id: child strategy 'fpe' != parent strategy 'hash'. Self-masking requires identical strategies. Update the child strategy to 'hash'.",
    ),
    "parent_not_root": (
        "chunked_fk_parent_not_root",
        "tables.customers.columns.id",
        "FK edge customers.id->orders.customer_id: customers.id is itself an FK child column elsewhere (a same-key cascade). The oracle FK-resolves it via its own parent map rather than masking it directly, so self-masking this edge's child would diverge from the resolved value. Use run_pipeline or run_sequential for a multi-hop same-key cascade.",
    ),
    "parent_when": (
        "chunked_fk_parent_when_unsupported",
        "tables.customers.columns.id.when",
        "FK edge customers.id->orders.customer_id: parent key column customers.id has an effective 'when'. The oracle masks this key conditionally (rows failing 'when' stay raw); self-masking has no 'when' and always masks, so the child would diverge on the gated rows. Remove 'when' from the parent key column, or use run_pipeline / run_sequential.",
    ),
    "child_when": (
        "chunked_fk_child_when_unsupported",
        "tables.orders.columns.customer_id.when",
        "FK edge customers.id->orders.customer_id: child key column orders.customer_id has an effective 'when'. The oracle ignores 'when' on an FK-resolved child column, but self-masking would honor it and mask only the rows that pass, diverging from the oracle on the gated rows. Remove 'when' from the child key column, or use run_pipeline / run_sequential.",
    ),
    "parent_provider": (
        "chunked_fk_endpoint_not_scalar",
        "tables.customers.columns.id.provider",
        "FK edge customers.id->orders.customer_id: parent key column customers.id declares a provider. A provider can route a hash-declared key through composite bundle generation instead of the plain hash handler, which this gate cannot verify is safe. Remove the provider from the parent key column, or use run_pipeline / run_sequential.",
    ),
    "child_provider": (
        "chunked_fk_endpoint_not_scalar",
        "tables.orders.columns.customer_id.provider",
        "FK edge customers.id->orders.customer_id: child key column orders.customer_id declares a provider. A provider can route a hash-declared key through composite bundle generation instead of the plain hash handler, which this gate cannot verify is safe. Remove the provider from the child key column, or use run_pipeline / run_sequential.",
    ),
    "provider_config_mismatch": (
        "chunked_fk_child_config_mismatch",
        "tables.orders.columns.customer_id.provider_config",
        "FK edge customers.id->orders.customer_id: child provider_config {'k': 1} != parent provider_config {}. Both declare strategy 'hash' but with different value-affecting settings, so self-masking the child independently would not reproduce the parent's masked value. Make the child provider_config identical to the parent's.",
    ),
    "dtype_unprovable": (
        "chunked_fk_child_key_dtype_unprovable",
        "tables.orders.columns.customer_id.dtype",
        "FK edge customers.id->orders.customer_id: strategy 'hash' is value-sensitive, so chunked self-masking reproduces the parent's masked value only when the parent and child FK key columns share a dtype family. That cannot be proven here: parent dtype 'date64', child dtype None (an undeclared dtype is not knowable at compile time, and the child's chunked run never sees the parent's data to check at runtime). Declare a matching 'dtype' on both FK key columns, or use run_pipeline / run_sequential (which normalize FK key equality via the parent map).",
    ),
    "dtype_family_mismatch": (
        "chunked_fk_child_key_dtype_mismatch",
        "tables.orders.columns.customer_id.dtype",
        "FK edge customers.id->orders.customer_id: child dtype 'float64' is not the same family as parent dtype 'date64'. Chunked self-masking hashes/truncates/FPEs the child's own value independently of the parent, with no parent-map lookup to normalize FK key equality; a dtype-family mismatch (e.g. parent int vs child float) means the child's raw value differs byte-for-byte from the parent's even when both represent the 'same' logical key, so self-masking cannot be guaranteed to reproduce the parent's masked value. Use run_pipeline or run_sequential instead, or normalize both columns to the same dtype before masking.",
    ),
    "parent_dtype_not_safe": (
        "chunked_fk_key_dtype_not_cross_adapter_safe",
        "tables.customers.columns.id.dtype",
        "FK edge customers.id->orders.customer_id: customers.id declares FK key dtype 'date64', which is not in the exact cross-adapter-safe set for hash-only FK self-masking (string, large_string, any integer width, bool, date32, an IANA-tz timestamp, or a 32/64/128-bit decimal with scale >= 0). hash is only proven cross-adapter byte-identical for these dtypes. Declare one of them, or use run_pipeline / run_sequential.",
    ),
    "child_dtype_not_safe": (
        "chunked_fk_key_dtype_not_cross_adapter_safe",
        "tables.orders.columns.customer_id.dtype",
        "FK edge customers.id->orders.customer_id: orders.customer_id declares FK key dtype 'int64[pyarrow]', which is not in the exact cross-adapter-safe set for hash-only FK self-masking (string, large_string, any integer width, bool, date32, an IANA-tz timestamp, or a 32/64/128-bit decimal with scale >= 0). hash is only proven cross-adapter byte-identical for these dtypes. Declare one of them, or use run_pipeline / run_sequential.",
    ),
}


def test_base_config_is_admitted() -> None:
    assert gate_fk_child_edges(_base_config(), table="orders") is None


def test_expected_table_covers_every_breaker_in_order() -> None:
    assert list(_EXPECTED) == [name for name, _ in _BREAKERS]


@pytest.mark.parametrize("k", range(len(_BREAKERS)), ids=[name for name, _ in _BREAKERS])
def test_earliest_violated_predicate_decides(k: int) -> None:
    err = _reject(_config_breaking_from(k))
    assert (err.code, err.path, err.message) == _EXPECTED[_BREAKERS[k][0]]


def test_table_that_is_only_a_parent_is_not_gated() -> None:
    cfg = _base_config()
    _edge(cfg)["orphan_policy"] = "warn"
    assert gate_fk_child_edges(cfg, table="customers") is None


def test_non_dict_relationship_and_child_entries_are_skipped() -> None:
    cfg = _base_config()
    cfg["relationships"].insert(0, "junk")
    cfg["relationships"][1]["children"].insert(0, 7)
    assert gate_fk_child_edges(cfg, table="orders") is None


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda c: _parent(c).__setitem__("when", "   "), id="blank_parent_when"),
        pytest.param(lambda c: _child(c).__setitem__("when", ""), id="empty_child_when"),
        pytest.param(
            lambda c: _parent(c).__setitem__("provider", None), id="explicit_none_provider"
        ),
        pytest.param(
            lambda c: (
                _parent(c).__setitem__("provider_config", None),
                _child(c).__setitem__("provider_config", {}),
            ),
            id="none_and_empty_provider_config",
        ),
        pytest.param(lambda c: _edge(c).__setitem__("namespace", "ns"), id="edge_ns_matches"),
        pytest.param(lambda c: _child(c).__setitem__("dtype", "uint8"), id="same_int_family"),
    ],
)
def test_near_misses_are_admitted(mutate: Callable[[Config], Any]) -> None:
    cfg = _base_config()
    mutate(cfg)
    assert gate_fk_child_edges(cfg, table="orders") is None


def test_parent_absent_from_tables_reads_as_no_strategy() -> None:
    cfg = _base_config()
    del cfg["tables"][1]
    err = _reject(cfg)
    assert (err.code, err.path) == (
        "chunked_fk_parent_strategy_not_self_mask_safe",
        "tables.customers.columns.id.strategy",
    )


def test_string_child_columns_count_as_composite() -> None:
    cfg = _base_config()
    _edge(cfg)["children"][0]["columns"] = "customer_id"
    err = _reject(cfg)
    assert (err.code, err.path) == (
        "chunked_fk_composite_unsupported",
        "relationships[customers.['id']->orders.customer_id].columns",
    )


def test_missing_parent_entry_counts_as_composite() -> None:
    cfg = _base_config()
    _edge(cfg)["parent"] = None
    err = _reject(cfg)
    assert (err.code, err.path) == (
        "chunked_fk_composite_unsupported",
        "relationships[None.[]->orders.['customer_id']].columns",
    )


def test_parent_that_is_a_component_of_an_upstream_composite_child_is_not_root() -> None:
    cfg = _base_config()
    cfg["relationships"].append(
        {
            "parent": {"table": "regions", "columns": ["id", "code"]},
            "children": [{"table": "customers", "columns": ["id", "region_code"]}],
            "orphan_policy": "remap",
        }
    )
    err = _reject(cfg)
    assert (err.code, err.path) == ("chunked_fk_parent_not_root", "tables.customers.columns.id")


def test_distinct_column_self_fk_is_admitted() -> None:
    col = {"strategy": "hash", "namespace": "ns", "dtype": "int64"}
    cfg = {
        "tables": [
            {"name": "employees", "columns": [{"name": "id", **col}, {"name": "manager_id", **col}]}
        ],
        "relationships": [
            {
                "parent": {"table": "employees", "columns": ["id"]},
                "children": [{"table": "employees", "columns": ["manager_id"]}],
                "orphan_policy": "remap",
            }
        ],
    }
    assert gate_fk_child_edges(cfg, table="employees") is None


def _two_edges_into_orders() -> Config:
    cfg = _base_config()
    cfg["tables"].append(
        {"name": "stores", "columns": [{"name": "id", "strategy": "hash", "namespace": "ns"}]}
    )
    cfg["tables"][2]["columns"].append(
        {"name": "store_id", "strategy": "hash", "namespace": "ns", "dtype": "int64"}
    )
    cfg["relationships"].append(
        {
            "parent": {"table": "stores", "columns": ["id"]},
            "children": [{"table": "orders", "columns": ["store_id"]}],
            "orphan_policy": "warn",
        }
    )
    return cfg


def test_edges_are_gated_in_relationship_order() -> None:
    cfg = _two_edges_into_orders()
    err = _reject(cfg)
    assert err.path == "relationships[stores.['id']->orders.['store_id']].orphan_policy"
    # A failure on the first edge wins over any failure on a later edge.
    _child(cfg)["provider"] = "p"
    err = _reject(cfg)
    assert (err.code, err.path) == (
        "chunked_fk_endpoint_not_scalar",
        "tables.orders.columns.customer_id.provider",
    )


def test_non_string_dtypes_escape_as_attribute_error() -> None:
    # Current behavior: a non-string declared dtype reaches the family check and
    # raises AttributeError instead of a PlanCompileError.
    cfg = _base_config()
    _parent(cfg)["dtype"] = 5
    _child(cfg)["dtype"] = 5
    with pytest.raises(AttributeError):
        gate_fk_child_edges(cfg, table="orders")


def test_non_dict_parent_entry_escapes_as_attribute_error() -> None:
    cfg = _base_config()
    _edge(cfg)["parent"] = ["customers"]
    with pytest.raises(AttributeError):
        gate_fk_child_edges(cfg, table="orders")
