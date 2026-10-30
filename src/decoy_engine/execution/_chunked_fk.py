"""FK self-masking gate for chunked execution (Option 1).

`gate_fk_child_edges` is the compile-time half: it walks every FK edge into
the gated table and runs the ordered `EDGE_CHECKS` predicate table
(`_chunked_fk_edge_checks.py`) on each. The per-chunk runtime half (the
lossy-passthrough guard and the FK column sets the dtype guards read) lives
in `_chunked_fk_runtime.py`, and the shared strategy and dtype-family
vocabulary in `_chunked_fk_vocab.py`; this module re-exports both so callers
keep one import point.

See docs/relationships-memory-scaling.md §2 for the full design and
_chunked.py module docstring for the gate conditions summary.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from decoy_engine.execution._chunked_fk_edge_checks import EDGE_CHECKS, FkEdge

# Callers and tests import these from here; they live in the runtime and vocab modules.
from decoy_engine.execution._chunked_fk_runtime import (
    fk_hash_strategy_columns_for_table,  # noqa: F401
    fk_passthrough_columns_for_table,  # noqa: F401
    reject_lossy_chunked_fk_passthrough,  # noqa: F401
)
from decoy_engine.execution._chunked_fk_vocab import (
    _DECIMAL_UNPROVABLE_FAMILY,  # noqa: F401
    CHUNK_SAFE_STRATEGIES,  # noqa: F401
    DTYPE_INVARIANT_STRATEGIES,  # noqa: F401
    NAMESPACE_REQUIRING_STRATEGIES,  # noqa: F401
    _dtype_family,  # noqa: F401
)


def _col_index_from_config(config: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Build a (table_name, col_name) -> col_entry lookup from config.tables."""
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for tbl in config.get("tables") or []:
        if not isinstance(tbl, dict):
            continue
        tbl_name = tbl.get("name")
        if not isinstance(tbl_name, str):
            continue
        for col in tbl.get("columns") or []:
            if isinstance(col, dict):
                col_name = col.get("name")
                if isinstance(col_name, str):
                    idx[(tbl_name, col_name)] = col
    return idx


def _child_endpoints_from_config(
    config: dict[str, Any],
) -> set[tuple[str, tuple[str, ...]]]:
    """Every `(child_table, child_columns)` key node across ALL relationships
    in `config`, regardless of which table is currently being gated.

    Predicate 8 needs this global view -- a parent key node is rejected when
    IT is itself a child endpoint SOMEWHERE in the config (a same-key cascade
    A -> B -> C), not just within the edge(s) into the table `gate_fk_child_
    edges` was called for. Precomputing it once here (rather than checking
    per-edge against only the CURRENT relationship entry) makes the predicate
    order-independent: it gives the same answer no matter which table in the
    chain gets gated first.
    """
    endpoints: set[tuple[str, tuple[str, ...]]] = set()
    for rel_entry in config.get("relationships") or []:
        if not isinstance(rel_entry, dict):
            continue
        for child_info in rel_entry.get("children") or []:
            if not isinstance(child_info, dict):
                continue
            child_table = child_info.get("table")
            child_cols = child_info.get("columns") or []
            if isinstance(child_table, str):
                endpoints.add((child_table, tuple(str(c) for c in child_cols)))
    return endpoints


def gate_fk_child_edges(config: dict[str, Any], *, table: str) -> None:
    """Gate each FK edge where `table` is the child against the self-mask conditions.

    Walks config.relationships and checks every edge whose child_table matches
    `table`. An edge is ADMITTED only when every condition below holds:

    (a) Parent key strategy is EXACTLY `hash`. Every other CHUNK_SAFE_STRATEGIES
        member (passthrough, fpe, redact, truncate, text_redact, date_shift,
        bucketize, top_code, text_mask) is a strategy x representation x
        substrate hole for FK self-masking specifically (2026-09-02 cascade-
        safety plan): each either depends on cascade/`when` state, whole-column
        state, or value representation/execution substrate in a way that makes
        the child's independent self-mask diverge from the join oracle's output
        for the SAME logical key. hash is the one strategy proven cross-adapter
        byte-identical (via the shared kernel canonicalizer), and only for the
        exact dtype set predicate 12 (below) admits.
    (b) Child FK column explicitly declares the same value-keyed strategy.
    (c) ONLY when parent strategy is in NAMESPACE_REQUIRING_STRATEGIES (hash,
        fpe, date_shift): child FK column explicitly declares the same namespace
        as the parent COLUMN. The parent-column namespace is the authoritative
        masking key: ColumnSeed.namespace = col_entry.get("namespace")
        (_seed_envelope.py:260). A config whose edge namespace disagrees with the
        parent-column namespace is rejected (chunked_fk_parent_namespace_mismatch)
        because the edge namespace is a label only and is never used to derive a
        masked value. A parent column with no namespace is rejected fail-closed
        (chunked_fk_parent_namespace_missing), consistent with the per-strategy
        namespace-required error at execution time.
        For namespace-agnostic strategies (redact, truncate, text_redact,
        bucketize, top_code, passthrough, text_mask), namespace sub-checks are skipped
        entirely:
        their output is pure(value, config) and byte-identical regardless of
        whether a namespace is declared. (After (a)'s narrowing, only hash ever
        reaches this point, so these strategies never actually exercise this
        sub-check on the FK route anymore -- retained for the general
        namespace-requiring-strategy contract this predicate documents.)
    (d) orphan_policy is 'remap'.
    (8) The parent KEY NODE `(parent_table, (parent_col,))` is not ITSELF an FK
        child endpoint anywhere in `config.relationships` (e.g. a same-key
        chain A -> B -> C). If it were, the oracle FK-RESOLVES the parent key
        (possibly to None on an upstream cascade failure) instead of masking it
        by its declared strategy, so self-masking the grandchild would compute
        `hash(value)` where the oracle computes `hash(resolved_parent_value)` --
        a silent divergence. Checked against every `(child_table, child_columns)`
        endpoint across the WHOLE config (not just edges into `table`), so the
        result is independent of which table happens to be gated first. A
        distinct-column self-FK on the same table (`employees.id ->
        employees.manager_id`) is NOT caught by this -- the key NODE
        `(employees, ("id",))` differs from `(employees, ("manager_id",))`, so
        `id` stays a root key even though `manager_id` on the same table is a
        child column.
    (9/10) Neither the parent nor the child FK key column has an EFFECTIVE
        `when` (a non-blank string, mirroring the compiler's own normalization
        at `plan/_seed_envelope.py:245`). A row-gated key is masked
        conditionally by the oracle (some rows stay raw); self-masking ignores
        `when` entirely and always masks, so a `when`-bearing FK key silently
        diverges on the gated rows.
    (11) Neither FK endpoint declares a `provider`. A provider can route even a
        hash-declared key through composite bundle generation
        (`_runner.py`/`_pandas_adapter.py`) instead of the plain hash handler;
        this gate has no registry to check which providers are safe, so any
        provider on an FK key is rejected conservatively.
    (12) The FK key dtype is in the EXACT cross-adapter-safe set (declared
        stage; see `_chunked_fk_dtype_safety.py`). This is intentionally
        NARROWER than the existing dtype-family comparison in the block below
        (which admits `date64`-as-`date32`, `decimal256`-as-`decimal128`, and a
        fixed-offset tz as IANA) -- hash is cross-adapter byte-identical only
        for the precise dtype set that module defines. This is the DECLARED
        half of a two-stage check; the per-chunk runtime guard
        (`_chunked_fk_dtype.reject_mismatched_chunked_fk_declared_dtype`)
        re-validates the REAL Arrow type at each chunk boundary, since this
        declared check -- like the family check beside it -- only ever runs
        when `table` is the CHILD of the edge being walked.

    First cut: single-column edges only; composite FK rejected.
    Tables that are FK parents but not children are not gated here (except
    predicate 8, which reads their child-endpoint identity from the config
    directly rather than requiring them to be the currently-gated table).

    Raises:
        PlanCompileError: if any condition fails (fail closed).
            chunked_fk_orphan_policy_not_remap: orphan_policy is not 'remap'.
            chunked_fk_composite_unsupported: FK edge has more than one column.
            chunked_fk_parent_strategy_not_self_mask_safe: parent strategy is
                not exactly 'hash'.
            chunked_fk_parent_namespace_missing: parent strategy is in
                NAMESPACE_REQUIRING_STRATEGIES and parent column has no namespace.
            chunked_fk_parent_namespace_mismatch: parent strategy is in
                NAMESPACE_REQUIRING_STRATEGIES and edge namespace disagrees with
                the parent-column namespace.
            chunked_fk_child_namespace_missing: parent strategy is in
                NAMESPACE_REQUIRING_STRATEGIES and child column has no namespace.
            chunked_fk_child_namespace_mismatch: parent strategy is in
                NAMESPACE_REQUIRING_STRATEGIES and child namespace does not match
                the parent-column namespace.
            chunked_fk_child_strategy_missing: child column has no strategy.
            chunked_fk_child_strategy_mismatch: child strategy differs from
                the parent strategy.
            chunked_fk_child_config_mismatch: child provider_config differs
                from the parent's (same strategy name but different
                value-affecting settings, e.g. redact_with, truncate length,
                hash truncation), so self-masking would NOT reproduce the
                parent's masked value even though the strategy name matches.
            chunked_fk_child_key_dtype_mismatch: parent and child FK key
                columns declare different dtype families (e.g. int vs
                float), so self-masking the child's own value independently
                cannot be guaranteed to reproduce the parent's masked value
                even when strategy, namespace, and provider_config all match.
            chunked_fk_child_key_dtype_unprovable: a value-sensitive strategy
                (anything but redact) does not have BOTH FK key dtypes declared,
                so identical masked values cannot be proven. Fail closed: an
                undeclared dtype is not knowable, and the child's chunked run
                never sees the parent's data to check at runtime.
            chunked_fk_parent_not_root: predicate 8 (parent key node is itself
                an FK child endpoint elsewhere).
            chunked_fk_parent_when_unsupported / chunked_fk_child_when_unsupported:
                predicates 9/10 (an effective `when` on the parent/child key).
            chunked_fk_endpoint_not_scalar: predicate 11 (a `provider` on either
                FK key endpoint).
            chunked_fk_key_dtype_not_cross_adapter_safe: predicate 12, declared
                stage (an FK key dtype outside the exact cross-adapter-safe
                set).
    """
    col_index = _col_index_from_config(config)
    # Decompose every child endpoint to its component (table, column) pairs.
    # Predicate 8 matches on components, not whole tuples: composite FK
    # resolution rewrites EVERY participating child column, so a scalar parent
    # that is ANY component of an upstream composite child endpoint is NOT a
    # root and must be rejected. A scalar child endpoint decomposes to itself,
    # so this also subsumes the same-key scalar cascade.
    child_endpoint_columns = {
        (child_table, child_col)
        for child_table, child_cols in _child_endpoints_from_config(config)
        for child_col in child_cols
    }
    for edge in _edges_into(config, table, col_index, child_endpoint_columns):
        for check in EDGE_CHECKS:
            if check.rejects(edge):
                raise check.error(edge)


def _edges_into(
    config: dict[str, Any],
    table: str,
    col_index: dict[tuple[str, str], dict[str, Any]],
    child_endpoint_columns: set[tuple[str, str]],
) -> Iterator[FkEdge]:
    """Every edge whose child is `table`, in config order. Lazy, so an edge is
    fully gated before the next relationship entry is read."""
    for rel_entry in config.get("relationships") or []:
        if not isinstance(rel_entry, dict):
            continue
        parent_info = rel_entry.get("parent") or {}
        parent_table = parent_info.get("table")
        parent_cols = parent_info.get("columns") or []
        rel_ns: str | None = rel_entry.get("namespace")
        orphan_policy = rel_entry.get("orphan_policy")

        for child_info in rel_entry.get("children") or []:
            if not isinstance(child_info, dict):
                continue
            child_table = child_info.get("table")
            child_cols = child_info.get("columns") or []
            if child_table != table:
                continue
            yield FkEdge(
                parent_table=parent_table,
                parent_cols=parent_cols,
                child_table=child_table,
                child_cols=child_cols,
                rel_ns=rel_ns,
                orphan_policy=orphan_policy,
                col_index=col_index,
                child_endpoint_columns=child_endpoint_columns,
            )
