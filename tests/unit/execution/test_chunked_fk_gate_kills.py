"""Mutation-kill coverage for `gate_fk_child_edges` (the chunked-route FK
admission gate in `execution/_chunked_fk.py`).

The three existing selection files exercise a handful of reject codes end to
end but leave most of the gate's distinct reject branches -- and every one of
their `code`/`path`/`message` machine fields -- untripped. This file calls
`gate_fk_child_edges(config, table=...)` directly with raw dict configs (the
fastest way to reach each branch, and the only way to feed the nameless /
missing-column shapes that `PipelineConfig` validation would reject upstream)
and pins, for each reject branch:

  * the `.code` (the machine-routable field callers switch on),
  * the `.path` (the exact config location; kills the `path=None` mutants), and
  * the DATA the message carries -- the offending table/column/strategy/dtype
    NAME(s), and any joined list with its exact separator (which kills the
    `', '`->`'XX, XX'` separator mutant). A nulled message is caught too: the
    name substring test raises on `in None`.

House style (consistent with the rest of the TQ sweep): the message's free-text
EXPLANATORY PROSE is NOT asserted, so pure-prose mutants (an XX-wrapped or
re-cased explanatory sentence) survive as EQUIVALENTS -- prose carries no
machine contract, and a full-equality assertion would be brittle against any
legitimate reword. Only the machine fields and the data the message carries are
pinned.

Behavioral tests below cover the control-flow mutants (`continue`->`break`,
`or`->`and`, and the `col_index.get(..., {})` default nulled to None) that a
field assertion cannot see.
"""

from __future__ import annotations

import pytest

from decoy_engine.execution._chunked_fk import gate_fk_child_edges
from decoy_engine.plan import PlanCompileError

# path_prefix for the canonical customers.id -> orders.customer_id single-column
# edge (parent_cols/child_cols render as their list repr in the f-string).
_PREFIX = "relationships[customers.['id']->orders.['customer_id']]"


def _config(
    *,
    parent_col: dict | None,
    child_col: dict,
    orphan_policy: str = "remap",
    rel_ns: str | None = None,
) -> dict:
    """customers.id -> orders.customer_id single-column FK config. `parent_col`
    is the customers.id column entry (None omits the customers table entirely,
    to exercise a parent column absent from the col_index); `child_col` is the
    orders.customer_id entry."""
    tables: list[dict] = []
    if parent_col is not None:
        tables.append({"name": "customers", "columns": [parent_col]})
    tables.append({"name": "orders", "columns": [child_col]})
    rel: dict = {
        "parent": {"table": "customers", "columns": ["id"]},
        "children": [{"table": "orders", "columns": ["customer_id"]}],
        "orphan_policy": orphan_policy,
    }
    if rel_ns is not None:
        rel["namespace"] = rel_ns
    return {"global_settings": {"seed": 7}, "tables": tables, "relationships": [rel]}


def _reject(config: dict, table: str = "orders") -> PlanCompileError:
    with pytest.raises(PlanCompileError) as exc:
        gate_fk_child_edges(config, table=table)
    return exc.value


# ---------------------------------------------------------------------------
# Condition (d): orphan_policy must be 'remap'.
# Kills path (mut_53), path_prefix nulled (mut_48), and the message-null
# (mut_54, via the name substring). The prose sentences (mut_60-68) are left
# as equivalents.
# ---------------------------------------------------------------------------


def test_orphan_policy_not_remap_fields() -> None:
    cfg = _config(
        parent_col={"name": "id", "strategy": "passthrough", "dtype": "int64"},
        child_col={"name": "customer_id", "strategy": "passthrough", "dtype": "int64"},
        orphan_policy="warn",
    )
    err = _reject(cfg)
    assert err.code == "chunked_fk_orphan_policy_not_remap"
    assert err.path == f"{_PREFIX}.orphan_policy"
    # Data carried: the edge's table names and the offending policy value.
    assert "customers->orders" in err.message
    assert "got 'warn'" in err.message


# ---------------------------------------------------------------------------
# Composite FK edge rejected. Kills path (mut_75), the message-null (mut_76),
# AND the `or`->`and` mutation (mut_69): a 2-parent / 1-child edge trips the
# reject only under `or`; under `and` it slips through. Prose (mut_82-86) is
# left equivalent.
# ---------------------------------------------------------------------------


def test_composite_fk_fields_and_or_mutation() -> None:
    # parent has 2 key columns, child has 1: len(parent)!=1 is True, so `or`
    # rejects but `and` (needs both sides != 1) would not.
    cfg = {
        "global_settings": {"seed": 7},
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {"name": "id", "strategy": "passthrough", "dtype": "int64"},
                    {"name": "id2", "strategy": "passthrough", "dtype": "int64"},
                ],
            },
            {"name": "orders", "columns": [{"name": "customer_id", "strategy": "passthrough"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id", "id2"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "remap",
            }
        ],
    }
    err = _reject(cfg)
    assert err.code == "chunked_fk_composite_unsupported"
    assert err.path == "relationships[customers.['id', 'id2']->orders.['customer_id']].columns"
    # Data carried: the edge's table names.
    assert "customers->orders" in err.message


# ---------------------------------------------------------------------------
# Condition (a): parent strategy must be chunk-safe. Kills path (mut_105), the
# message-null (mut_106), and the join-separator mutant (mut_116, via the
# two-element list slice). The prose case-mutants (mut_112-114) are equivalent.
# ---------------------------------------------------------------------------


def test_parent_strategy_not_safe_fields() -> None:
    cfg = _config(
        parent_col={"name": "id", "strategy": "shuffle"},
        child_col={"name": "customer_id", "strategy": "shuffle"},
    )
    err = _reject(cfg)
    assert err.code == "chunked_fk_parent_strategy_not_safe"
    assert err.path == "tables.customers.columns.id.strategy"
    # Data carried: the edge names, the offending strategy value, and the full
    # sorted chunk-safe list joined with ", " -- asserting a two-element slice
    # of that join pins the separator and kills the `', '`->`'XX, XX'` mutant.
    assert "customers.id->orders.customer_id" in err.message
    assert "strategy 'shuffle'" in err.message
    assert "bucketize, date_shift" in err.message


# ---------------------------------------------------------------------------
# Condition (c1): parent column has no namespace under a namespace-requiring
# strategy. Kills path (mut_131) and the message-null (mut_132); prose
# (mut_138-142) is equivalent.
# ---------------------------------------------------------------------------


def test_parent_namespace_missing_fields() -> None:
    cfg = _config(
        parent_col={"name": "id", "strategy": "hash", "dtype": "int64"},
        child_col={"name": "customer_id", "strategy": "hash", "dtype": "int64"},
    )
    err = _reject(cfg)
    assert err.code == "chunked_fk_parent_namespace_missing"
    assert err.path == "tables.customers.columns.id.namespace"
    # Data carried: the edge names and the offending strategy value.
    assert "customers.id->orders.customer_id" in err.message
    assert "strategy 'hash'" in err.message


# ---------------------------------------------------------------------------
# Condition (c2): edge namespace disagrees with the parent-column namespace.
# Kills path (mut_147); this branch has no message-null mutant, and the prose
# (mut_154-157) is equivalent.
# ---------------------------------------------------------------------------


def test_parent_namespace_mismatch_fields() -> None:
    cfg = _config(
        parent_col={"name": "id", "strategy": "hash", "namespace": "pns", "dtype": "int64"},
        child_col={"name": "customer_id", "strategy": "hash", "namespace": "pns", "dtype": "int64"},
        rel_ns="rns",
    )
    err = _reject(cfg)
    assert err.code == "chunked_fk_parent_namespace_mismatch"
    assert err.path == f"{_PREFIX}.namespace"
    # Data carried: the edge names and the two conflicting namespace values.
    assert "customers.id->orders.customer_id" in err.message
    assert "namespace 'rns'" in err.message
    assert "namespace 'pns'" in err.message


# ---------------------------------------------------------------------------
# Condition (c3a): child column has no namespace. Kills path (mut_164) and the
# message-null (mut_165); prose (mut_171-176) is equivalent.
# ---------------------------------------------------------------------------


def test_child_namespace_missing_fields() -> None:
    cfg = _config(
        parent_col={"name": "id", "strategy": "hash", "namespace": "pns", "dtype": "int64"},
        child_col={"name": "customer_id", "strategy": "hash", "dtype": "int64"},
        rel_ns="pns",
    )
    err = _reject(cfg)
    assert err.code == "chunked_fk_child_namespace_missing"
    assert err.path == "tables.orders.columns.customer_id.namespace"
    # Data carried: the edge names and the parent namespace the hint echoes.
    assert "customers.id->orders.customer_id" in err.message
    assert "'namespace: pns'" in err.message


# ---------------------------------------------------------------------------
# Condition (c3b): child namespace differs from the parent-column namespace.
# Kills path (mut_179) and the message-null (mut_180); prose (mut_186-190) is
# equivalent.
# ---------------------------------------------------------------------------


def test_child_namespace_mismatch_fields() -> None:
    cfg = _config(
        parent_col={"name": "id", "strategy": "hash", "namespace": "pns", "dtype": "int64"},
        child_col={"name": "customer_id", "strategy": "hash", "namespace": "cns", "dtype": "int64"},
        rel_ns="pns",
    )
    err = _reject(cfg)
    assert err.code == "chunked_fk_child_namespace_mismatch"
    assert err.path == "tables.orders.columns.customer_id.namespace"
    # Data carried: the edge names and the two conflicting namespace values.
    assert "customers.id->orders.customer_id" in err.message
    assert "child namespace 'cns'" in err.message
    assert "namespace 'pns'" in err.message


# ---------------------------------------------------------------------------
# Condition (b1): child column declares no strategy. Kills path (mut_197) and
# the message-null (mut_198); prose (mut_204-208) is equivalent.
# ---------------------------------------------------------------------------


def test_child_strategy_missing_fields() -> None:
    cfg = _config(
        parent_col={"name": "id", "strategy": "passthrough", "dtype": "int64"},
        child_col={"name": "customer_id", "dtype": "int64"},  # no strategy
    )
    err = _reject(cfg)
    assert err.code == "chunked_fk_child_strategy_missing"
    assert err.path == "tables.orders.columns.customer_id.strategy"
    # Data carried: the edge names and the parent strategy the hint echoes.
    assert "customers.id->orders.customer_id" in err.message
    assert "'strategy: passthrough'" in err.message


# ---------------------------------------------------------------------------
# Condition (b2): child strategy differs from parent strategy. Kills path
# (mut_211) and the message-null (mut_212, via the name substring).
# ---------------------------------------------------------------------------


def test_child_strategy_mismatch_fields() -> None:
    cfg = _config(
        parent_col={"name": "id", "strategy": "passthrough", "dtype": "int64"},
        child_col={"name": "customer_id", "strategy": "hash", "dtype": "int64"},
    )
    err = _reject(cfg)
    assert err.code == "chunked_fk_child_strategy_mismatch"
    assert err.path == "tables.orders.columns.customer_id.strategy"
    # Data carried: the edge names and the two conflicting strategy values.
    assert "customers.id->orders.customer_id" in err.message
    assert "child strategy 'hash' != parent strategy 'passthrough'" in err.message


# ---------------------------------------------------------------------------
# Condition (e): child provider_config differs from the parent's. redact is
# namespace-agnostic and dtype-invariant, so the edge reaches (e) directly.
# Kills path (mut_230); this branch has no message-null mutant, and the prose
# (mut_237-245) is equivalent.
# ---------------------------------------------------------------------------


def test_child_provider_config_mismatch_fields() -> None:
    cfg = _config(
        parent_col={"name": "id", "strategy": "redact", "provider_config": {"redact_with": "P"}},
        child_col={
            "name": "customer_id",
            "strategy": "redact",
            "provider_config": {"redact_with": "C"},
        },
    )
    err = _reject(cfg)
    assert err.code == "chunked_fk_child_config_mismatch"
    assert err.path == "tables.orders.columns.customer_id.provider_config"
    # Data carried: the edge names, both provider_config dicts, and the strategy.
    assert "customers.id->orders.customer_id" in err.message
    assert "child provider_config {'redact_with': 'C'}" in err.message
    assert "provider_config {'redact_with': 'P'}" in err.message
    assert "strategy 'redact'" in err.message


# ---------------------------------------------------------------------------
# Condition (f1): value-sensitive strategy with an undeclared dtype. Kills path
# (mut_259) and the message-null (mut_260); prose (mut_266-286) is equivalent.
# ---------------------------------------------------------------------------


def test_child_key_dtype_unprovable_fields() -> None:
    cfg = _config(
        parent_col={"name": "id", "strategy": "passthrough"},  # no dtype
        child_col={"name": "customer_id", "strategy": "passthrough"},  # no dtype
    )
    err = _reject(cfg)
    assert err.code == "chunked_fk_child_key_dtype_unprovable"
    assert err.path == "tables.orders.columns.customer_id.dtype"
    # Data carried: the edge names, the strategy, and both (undeclared) dtypes.
    assert "customers.id->orders.customer_id" in err.message
    assert "strategy 'passthrough'" in err.message
    assert "parent dtype None, child dtype None" in err.message


# ---------------------------------------------------------------------------
# Condition (f2): declared dtypes are different families. Kills path (mut_291)
# and the message-null (mut_292); prose (mut_298-318) is equivalent.
# ---------------------------------------------------------------------------


def test_child_key_dtype_mismatch_fields() -> None:
    cfg = _config(
        parent_col={"name": "id", "strategy": "passthrough", "dtype": "int64"},
        child_col={"name": "customer_id", "strategy": "passthrough", "dtype": "float64"},
    )
    err = _reject(cfg)
    assert err.code == "chunked_fk_child_key_dtype_mismatch"
    assert err.path == "tables.orders.columns.customer_id.dtype"
    # Data carried: the edge names and the two mismatched dtype values.
    assert "customers.id->orders.customer_id" in err.message
    assert "child dtype 'float64'" in err.message
    assert "parent dtype 'int64'" in err.message


# ---------------------------------------------------------------------------
# Control-flow mutants: `continue`->`break` on the three skip guards. Each test
# puts a SKIPPED entry first and a REJECTING entry second; `continue` reaches
# the rejecting entry (raises), `break` abandons the loop (no raise).
# ---------------------------------------------------------------------------


def test_non_dict_relationship_is_skipped_not_break() -> None:
    """mut_8: a non-dict relationship entry must be skipped so a later valid
    (rejecting) entry is still processed; `break` would abandon the loop."""
    cfg = {
        "global_settings": {"seed": 7},
        "tables": [
            {"name": "customers", "columns": [{"name": "id", "strategy": "shuffle"}]},
            {"name": "orders", "columns": [{"name": "customer_id", "strategy": "shuffle"}]},
        ],
        "relationships": [
            123,  # non-dict -> continue past it
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "remap",  # passes (d); rejected at (a) shuffle
            },
        ],
    }
    assert _reject(cfg).code == "chunked_fk_parent_strategy_not_safe"


def test_non_dict_child_is_skipped_not_break() -> None:
    """mut_36: a non-dict child entry must be skipped so a later valid
    (rejecting) child in the same edge is still processed."""
    cfg = {
        "global_settings": {"seed": 7},
        "tables": [
            {"name": "customers", "columns": [{"name": "id", "strategy": "shuffle"}]},
            {"name": "orders", "columns": [{"name": "customer_id", "strategy": "shuffle"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [
                    "not-a-dict",  # non-dict -> continue past it
                    {"table": "orders", "columns": ["customer_id"]},
                ],
                "orphan_policy": "remap",
            }
        ],
    }
    assert _reject(cfg).code == "chunked_fk_parent_strategy_not_safe"


def test_non_matching_child_is_skipped_not_break() -> None:
    """mut_47: a child whose table != the gated table must be skipped so a
    later matching (rejecting) child is still processed."""
    cfg = {
        "global_settings": {"seed": 7},
        "tables": [
            {"name": "customers", "columns": [{"name": "id", "strategy": "shuffle"}]},
            {"name": "orders", "columns": [{"name": "customer_id", "strategy": "shuffle"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [
                    {"table": "other_table", "columns": ["x"]},  # child_table != table
                    {"table": "orders", "columns": ["customer_id"]},
                ],
                "orphan_policy": "remap",
            }
        ],
    }
    assert _reject(cfg, table="orders").code == "chunked_fk_parent_strategy_not_safe"


# ---------------------------------------------------------------------------
# Dict-default mutants: `col_index.get(key, {})` nulled to `get(key, None)` (or
# the default dropped). When the looked-up column is absent from the index the
# original yields {} and the gate raises a typed PlanCompileError; the mutant
# yields None and the following `.get(...)` raises AttributeError instead.
# ---------------------------------------------------------------------------


def test_missing_parent_column_yields_typed_error_not_attributeerror() -> None:
    """mut_95 / mut_97: the parent key column is not declared in any table, so
    the parent lookup misses. With the {} default parent_strategy is None ->
    typed chunked_fk_parent_strategy_not_safe; with a None default the gate
    would raise AttributeError on None.get('strategy')."""
    cfg = {
        "global_settings": {"seed": 7},
        "tables": [
            # customers declares 'wrongname', NOT 'id' -> (customers, id) missing.
            {"name": "customers", "columns": [{"name": "wrongname", "strategy": "passthrough"}]},
            {"name": "orders", "columns": [{"name": "customer_id", "strategy": "passthrough"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "remap",
            }
        ],
    }
    assert _reject(cfg).code == "chunked_fk_parent_strategy_not_safe"


def test_missing_child_column_yields_typed_error_not_attributeerror() -> None:
    """mut_124 / mut_126: the child FK column is not declared, so the child
    lookup misses. With the {} default child_strategy is None -> typed
    chunked_fk_child_strategy_missing; with a None default the gate would raise
    AttributeError on None.get('strategy')."""
    cfg = {
        "global_settings": {"seed": 7},
        "tables": [
            {
                "name": "customers",
                "columns": [{"name": "id", "strategy": "passthrough", "dtype": "int64"}],
            },
            # orders declares 'wrongname', NOT 'customer_id' -> (orders, customer_id) missing.
            {"name": "orders", "columns": [{"name": "wrongname", "strategy": "passthrough"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "remap",
            }
        ],
    }
    assert _reject(cfg).code == "chunked_fk_child_strategy_missing"
