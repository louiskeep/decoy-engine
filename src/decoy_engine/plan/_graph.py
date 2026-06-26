"""Plan graph builders (relationships + namespaces), split from _compile.py (F11d).

Pure functions over (config, profile) producing the Plan-side relationship
and namespace tuples. Imported back into _compile.py so compile_plan and the
decoy_engine.plan._compile._build_relationships path are unchanged.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._types import (
    NamespaceBinding,
    PlanRelationship,
    PlanRelationshipEnd,
)
from decoy_engine.profile._types import Profile


def _build_relationships(
    config: dict[str, Any],
    profile: Profile,
    *,
    # Value is heterogeneous by producer: compile_plan passes the
    # relationships.OrphanPolicy enum, the fallback below builds plain str.
    # _normalised_policy() is the single normalizer for both shapes.
    orphan_policy_lookup: dict[tuple[str, tuple[str, ...], str, tuple[str, ...]], Any]
    | None = None,
) -> tuple[PlanRelationship, ...]:
    """Convert profile.relationships into Plan-side PlanRelationship tuples,
    pulling orphan_policy from the config when available.

    QA walks/generators F5 (2026-06-01, MEDIUM design): the orphan-policy
    lookup is passed in from compile_plan, where it was produced by
    check_orphan_fk_policy_completeness. Pre-fix this function re-parsed
    config.relationships independently; the two parses could drift.
    Single source of truth: check_orphan_fk_policy_completeness owns
    the parse; _build_relationships consumes the lookup.

    S13-rebaseline P1 (2026-06-01, BLOCKER fix): lookup key now
    includes child_table + child_cols so per-(parent, child) policies
    are honored. Pre-fix the lookup was per-parent only, which
    silently collapsed two children of the same parent with different
    policies into one (last-wins). The PlanRelationship schema carries
    one policy per entry, so different policies for different children
    now produce SEPARATE PlanRelationship entries grouped by
    (parent, policy).

    The fallback (orphan_policy_lookup=None) preserves the original
    parse logic so callers outside compile_plan still work.
    """
    if orphan_policy_lookup is None:
        # Fallback: reparse config.relationships (preserves pre-fix
        # behavior for callers that bypass compile_plan). Builds the
        # same per-(parent, child) key as check_orphan_fk_policy_completeness.
        orphan_policy_lookup = {}
        config_relationships = config.get("relationships", [])
        if isinstance(config_relationships, list):
            for entry in config_relationships:
                if not isinstance(entry, dict):
                    continue
                parent = entry.get("parent")
                policy = entry.get("orphan_policy")
                if not isinstance(parent, dict) or not policy:
                    continue
                parent_table = parent.get("table")
                parent_cols = parent.get("columns")
                if not (
                    isinstance(parent_table, str)
                    and isinstance(parent_cols, list)
                    and all(isinstance(c, str) for c in parent_cols)
                ):
                    continue
                children = entry.get("children", [])
                if not isinstance(children, list):
                    continue
                for child in children:
                    if not isinstance(child, dict):
                        continue
                    child_table = child.get("table")
                    child_cols = child.get("columns")
                    if (
                        isinstance(child_table, str)
                        and isinstance(child_cols, list)
                        and all(isinstance(c, str) for c in child_cols)
                    ):
                        orphan_policy_lookup[
                            (parent_table, tuple(parent_cols), child_table, tuple(child_cols))
                        ] = policy

    def _normalised_policy(value: Any) -> str:
        """Read the lookup result and produce a string literal that
        PlanRelationship.orphan_policy accepts. Handles OrphanPolicy
        enums (the check_orphan_fk_policy_completeness output shape)
        + falls back to 'preserve' for invalid values."""
        if hasattr(value, "value"):
            value = value.value
        if value not in ("preserve", "remap", "warn", "fail"):
            return "preserve"
        return str(value)

    # Group profile.relationships by (parent_table, parent_cols, policy)
    # so children with the same parent + same policy collapse into one
    # PlanRelationship entry, but different-policy children of the same
    # parent become SEPARATE entries (necessary post-S13-rebaseline-P1).
    grouped: dict[
        tuple[str, tuple[str, ...], str],
        list[tuple[str, tuple[str, ...], str | None]],
    ] = {}
    for rel in profile.relationships:
        per_rel_policy = _normalised_policy(
            orphan_policy_lookup.get(
                (rel.parent_table, rel.parent_columns, rel.child_table, rel.child_columns),
                "preserve",
            )
        )
        key = (rel.parent_table, rel.parent_columns, per_rel_policy)
        grouped.setdefault(key, []).append((rel.child_table, rel.child_columns, rel.namespace))

    out: list[PlanRelationship] = []
    for (parent_table, parent_cols, policy), children in sorted(grouped.items()):
        # All children of the same parent share a namespace if any do
        # (S2 enforces this at build_namespace_registry time; for S1 we
        # take the first non-None we see).
        namespace = next((ns for (_, _, ns) in children if ns is not None), None)
        out.append(
            PlanRelationship(
                parent=PlanRelationshipEnd(table=parent_table, columns=parent_cols),
                children=tuple(PlanRelationshipEnd(table=t, columns=c) for (t, c, _) in children),
                orphan_policy=policy,  # type: ignore[arg-type]
                namespace=namespace,
            )
        )
    return tuple(out)


def _build_namespaces(config: dict[str, Any]) -> tuple[NamespaceBinding, ...]:
    """Read namespaces from config and produce NamespaceBinding tuples.

    S1 only consumes config-declared namespaces. S2 auto-binds FK child
    columns into their parent's namespace; that promotion lives in
    build_namespace_registry.
    """
    out: list[NamespaceBinding] = []
    namespaces = config.get("namespaces", {})
    if not isinstance(namespaces, dict):
        return tuple()
    for ns_name, ns_body in sorted(namespaces.items()):
        if not isinstance(ns_body, dict):
            continue
        declared_strings = ns_body.get("declared_by", []) or []
        declared_by: list[tuple[str, tuple[str, ...]]] = []
        for entry in declared_strings:
            if not isinstance(entry, str) or "." not in entry:
                continue
            table, col = entry.split(".", 1)
            declared_by.append((table, (col,)))
        out.append(
            NamespaceBinding(
                namespace=ns_name,
                declared_by=tuple(declared_by),
            )
        )
    return tuple(out)
