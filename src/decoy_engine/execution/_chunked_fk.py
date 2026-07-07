"""FK self-masking gate for chunked execution (Option 1).

Extracted to keep _chunked.py under the orchestration LOC cap.
See docs/relationships-memory-scaling.md §2 for the full design and
_chunked.py module docstring for the gate conditions summary.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError

# Value-keyed strategies that produce the same output per (seed, namespace, value)
# regardless of row position or chunk boundary. Defined here and re-exported by
# _chunked.py so that the chunked-FK gate and the general compatibility check share
# one authoritative set without a circular import.
CHUNK_SAFE_STRATEGIES: frozenset[str] = frozenset(
    {
        "hash",
        "fpe",
        "redact",
        "truncate",
        "text_redact",
        "date_shift",
        "bucketize",
        "passthrough",
    }
)

# Subset of CHUNK_SAFE_STRATEGIES whose handlers call derive(job_seed, namespace, ...)
# and raise at execution when plan.namespace is None. Each raises a typed error code
# when the namespace is absent:
#   hash       -- hash_requires_namespace
#   fpe        -- fpe_requires_namespace
#   date_shift -- date_shift_requires_namespace
# The remaining CHUNK_SAFE strategies (redact, truncate, text_redact, bucketize,
# passthrough) do not read plan.namespace and produce byte-identical output
# regardless of whether a namespace is declared; the namespace sub-checks in the
# FK gate are skipped for them.
NAMESPACE_REQUIRING_STRATEGIES: frozenset[str] = frozenset({"hash", "fpe", "date_shift"})


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


def gate_fk_child_edges(config: dict[str, Any], *, table: str) -> None:
    """Gate each FK edge where `table` is the child against the self-mask conditions.

    Walks config.relationships and checks every edge whose child_table matches
    `table`. An edge is ADMITTED only when all four conditions hold:

    (a) Parent key strategy is in CHUNK_SAFE_STRATEGIES.
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
        bucketize, passthrough), namespace sub-checks are skipped entirely:
        their output is pure(value, config) and byte-identical regardless of
        whether a namespace is declared.
    (d) orphan_policy is 'remap'.

    First cut: single-column edges only; composite FK rejected.
    Tables that are FK parents but not children are not gated here.

    Raises:
        PlanCompileError: if any condition fails (fail closed).
            chunked_fk_orphan_policy_not_remap: orphan_policy is not 'remap'.
            chunked_fk_composite_unsupported: FK edge has more than one column.
            chunked_fk_parent_strategy_not_safe: parent strategy is not in
                CHUNK_SAFE_STRATEGIES.
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
    """
    col_index = _col_index_from_config(config)

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

            # Only gate edges where the current table is the FK child.
            if child_table != table:
                continue

            path_prefix = f"relationships[{parent_table}.{parent_cols}->{child_table}.{child_cols}]"

            # Condition (d): REMAP is the only policy byte-identical to self-masking.
            # REMAP mints via parent_strategy(seed, ns, orphan_key) == self-mask.
            # WARN/FAIL/PRESERVE require the parent key set resident.
            if orphan_policy != "remap":
                raise PlanCompileError(
                    code="chunked_fk_orphan_policy_not_remap",
                    path=f"{path_prefix}.orphan_policy",
                    message=(
                        f"FK edge {parent_table}->{child_table}: chunked self-masking "
                        f"requires orphan_policy 'remap'; got {orphan_policy!r}. "
                        "REMAP mints via the same parent strategy and namespace as "
                        "self-masking. WARN/FAIL/PRESERVE require the parent key set "
                        "resident. Use run_pipeline or run_sequential instead."
                    ),
                )

            # First cut: single-column FK edges only.
            if len(parent_cols) != 1 or len(child_cols) != 1:
                raise PlanCompileError(
                    code="chunked_fk_composite_unsupported",
                    path=f"{path_prefix}.columns",
                    message=(
                        f"FK edge {parent_table}->{child_table}: composite FK edges "
                        "(multi-column keys) are not supported in chunked self-masking. "
                        "Use run_pipeline or run_sequential instead."
                    ),
                )

            parent_col = str(parent_cols[0])
            child_col = str(child_cols[0])

            # Condition (a): parent strategy must be value-keyed and chunk-safe.
            parent_cfg = col_index.get((str(parent_table), parent_col), {})
            parent_strategy = parent_cfg.get("strategy")
            if parent_strategy not in CHUNK_SAFE_STRATEGIES:
                raise PlanCompileError(
                    code="chunked_fk_parent_strategy_not_safe",
                    path=f"tables.{parent_table}.columns.{parent_col}.strategy",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"parent key strategy {parent_strategy!r} is not chunk-safe. "
                        "Self-masking requires a value-keyed deterministic strategy. "
                        f"Chunk-safe: {', '.join(sorted(CHUNK_SAFE_STRATEGIES))}."
                    ),
                )

            # Condition (c): namespace sub-checks apply ONLY when the parent
            # strategy is in NAMESPACE_REQUIRING_STRATEGIES (hash, fpe, date_shift).
            # Those handlers call derive(job_seed, namespace, ...) and raise at
            # execution when plan.namespace is None.  Namespace-agnostic strategies
            # (redact, truncate, text_redact, bucketize, passthrough) do not read
            # plan.namespace; their output is pure(value, config) and byte-identical
            # regardless of whether a namespace is declared, so no namespace
            # sub-checks are needed and imposing them would over-reject configs that
            # the full-frame path accepts.
            #
            # Sub-checks for namespace-requiring strategies (all fail-closed):
            #   c1. Parent column must declare a namespace; the strategy raises at
            #       execution when plan.namespace is None.  Closed here so chunked
            #       is not more permissive than the full-frame path.
            #   c2. When rel_ns is set it must equal the parent-column namespace;
            #       a disagreement flags a mis-wiring (edge ns != masking ns).
            #   c3. Child namespace must equal the parent-column namespace;
            #       otherwise derive() uses a different key and output diverges.
            parent_ns: str | None = parent_cfg.get("namespace")
            child_cfg = col_index.get((str(child_table), child_col), {})
            if parent_strategy in NAMESPACE_REQUIRING_STRATEGIES:
                if parent_ns is None:
                    raise PlanCompileError(
                        code="chunked_fk_parent_namespace_missing",
                        path=f"tables.{parent_table}.columns.{parent_col}.namespace",
                        message=(
                            f"FK edge {parent_table}.{parent_col}"
                            f"->{child_table}.{child_col}: "
                            f"parent column has no namespace and strategy "
                            f"{parent_strategy!r} requires one. "
                            f"The {parent_strategy!r} strategy raises an error at "
                            "execution when no namespace is declared. "
                            "Add 'namespace: <ns>' to the parent column."
                        ),
                    )
                if rel_ns is not None and rel_ns != parent_ns:
                    raise PlanCompileError(
                        code="chunked_fk_parent_namespace_mismatch",
                        path=f"{path_prefix}.namespace",
                        message=(
                            f"FK edge {parent_table}.{parent_col}"
                            f"->{child_table}.{child_col}: "
                            f"edge namespace {rel_ns!r} disagrees with parent column "
                            f"namespace {parent_ns!r}. The parent column namespace is "
                            "the authoritative masking key; the edge namespace is a "
                            "label only and is never used to derive a masked value. "
                            f"Update the edge namespace to {parent_ns!r} or remove it."
                        ),
                    )

                # Child must explicitly declare the same namespace.
                # ColumnSeed.namespace is set from col_entry.get('namespace') directly;
                # FK auto-binding updates the NamespaceRegistry but NOT the ColumnSeed.
                child_ns: str | None = child_cfg.get("namespace")
                if child_ns is None:
                    raise PlanCompileError(
                        code="chunked_fk_child_namespace_missing",
                        path=f"tables.{child_table}.columns.{child_col}.namespace",
                        message=(
                            f"FK edge {parent_table}.{parent_col}"
                            f"->{child_table}.{child_col}: "
                            f"child column {child_table}.{child_col} has no explicit namespace. "
                            "ColumnSeed.namespace comes from the config column entry, not from "
                            "FK auto-binding. "
                            f"Add 'namespace: {parent_ns}' to the child column."
                        ),
                    )
                if child_ns != parent_ns:
                    raise PlanCompileError(
                        code="chunked_fk_child_namespace_mismatch",
                        path=f"tables.{child_table}.columns.{child_col}.namespace",
                        message=(
                            f"FK edge {parent_table}.{parent_col}"
                            f"->{child_table}.{child_col}: "
                            f"child namespace {child_ns!r} does not match parent column "
                            f"namespace {parent_ns!r}. "
                            "Self-masking requires child_ns == parent_ns so "
                            "derive(seed, ns, value) produces byte-identical output."
                        ),
                    )

            # Condition (b): child must explicitly declare the same strategy.
            # By-reference model (no child strategy) would stream raw FK keys.
            child_strategy: str | None = child_cfg.get("strategy")
            if child_strategy is None:
                raise PlanCompileError(
                    code="chunked_fk_child_strategy_missing",
                    path=f"tables.{child_table}.columns.{child_col}.strategy",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"child column {child_table}.{child_col} has no explicit strategy. "
                        "The by-reference model would stream raw FK key values without "
                        "the parent map resident. "
                        f"Add 'strategy: {parent_strategy}' to the child column."
                    ),
                )
            if child_strategy != parent_strategy:
                raise PlanCompileError(
                    code="chunked_fk_child_strategy_mismatch",
                    path=f"tables.{child_table}.columns.{child_col}.strategy",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"child strategy {child_strategy!r} != parent strategy "
                        f"{parent_strategy!r}. Self-masking requires identical strategies. "
                        f"Update the child strategy to {parent_strategy!r}."
                    ),
                )

            # Condition (e): child provider_config must be IDENTICAL to the
            # parent's, not just the same strategy name. Matching names alone
            # is not enough: e.g. redact_with='P' on the parent vs 'C' on the
            # child, or different truncate lengths / hash truncation, produce
            # different masked bytes even though both sides say "redact" or
            # "truncate". run_mask_pipeline_chunked self-masks the child
            # independently of the parent, so unless the two configs are
            # byte-for-byte identical, self-masking cannot be guaranteed to
            # reproduce the parent's masked value and FK referential
            # integrity would silently break.
            parent_provider_cfg = parent_cfg.get("provider_config") or {}
            child_provider_cfg = child_cfg.get("provider_config") or {}
            if parent_provider_cfg != child_provider_cfg:
                raise PlanCompileError(
                    code="chunked_fk_child_config_mismatch",
                    path=f"tables.{child_table}.columns.{child_col}.provider_config",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"child provider_config {child_provider_cfg!r} != parent "
                        f"provider_config {parent_provider_cfg!r}. Both declare "
                        f"strategy {parent_strategy!r} but with different "
                        "value-affecting settings, so self-masking the child "
                        "independently would not reproduce the parent's masked "
                        "value. Make the child provider_config identical to the "
                        "parent's."
                    ),
                )
