"""Namespace registry: resolves columns to their masking namespace.

The registry is the planning-layer data structure that answers "what
namespace does (table, columns) mask under?" for every column that
appears in the config or in a FK relationship. Two columns bound to the
same namespace mask to the same value; columns in different namespaces
mask independently even if the source values overlap.

The central invariant the registry enforces is **same-FK -> same-mask**:
when a FK edge declares a namespace, the child column auto-inherits
that namespace from the parent. Per the S2 spec TODO 2 resolution, an
explicit override of that inheritance is rejected as `namespace_ambiguity`
rather than silently producing post-mask joins that return zero rows.

Source pattern: URN-style registries in language servers (LSP textDocument
URI -> capabilities) and package managers (canonical package name ->
metadata), where a small, immutable lookup table maps a stable identifier
to its resolved scope. Same shape here: (table, columns) tuple ->
namespace string.

This module's `NamespaceBinding` is distinct from
`decoy_engine.plan._types.NamespaceBinding`: the plan-side type carries
the YAML-serializable `seed` field (S3 fills it with HKDF material); the
registry-side type only carries declaration metadata, since seeds live
in the seed envelope, not the registry.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile._types import Profile, Relationship


class NamespaceConfigError(PlanCompileError):
    """Raised by `build_namespace_registry` on namespace-resolution failures.

    Subclass of `PlanCompileError` per the S2 spec TODO 5 resolution:
    callers writing `except PlanCompileError` catch every compile-time
    failure including namespace ones, while `isinstance(e, NamespaceConfigError)`
    + `e.code` still discriminate the specific failure.

    Codes emitted by this module:

    - `namespace_ambiguity`: a column is declared in two namespaces, OR
      a child FK column is explicitly bound to a namespace other than its
      parent's inherited namespace. The override path is intentionally
      rejected; see the spec TODO 2 rationale (post-mask joins would
      return zero rows).
    - `namespace_missing`: a column requires a namespace and none can be
      resolved. Two clauses: (a) a deterministic-mode column in the config
      that does not declare a namespace; (b) a FK `Relationship` with
      `namespace=None` where neither parent nor child column declares
      one explicitly and no auto-binding supplies one.
    """


@dataclass(frozen=True)
class NamespaceBinding:
    """One namespace and the (table, columns) tuples bound to it.

    Composite columns appear as a single entry per binding: e.g.
    `(enrollments, (member_id, plan_id, effective_date))` is one entry,
    not three separate `member_id` / `plan_id` / `effective_date` ones.
    """

    namespace: str
    declared_by: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class NamespaceRegistry:
    """Runtime lookup table for (table, columns) -> namespace.

    Frozen by construction; rebuilt by `build_namespace_registry` on every
    compile. The `bindings` tuple is sorted by namespace name for
    deterministic iteration; query methods do not depend on order.
    """

    bindings: tuple[NamespaceBinding, ...]

    def for_column(self, table: str, columns: tuple[str, ...]) -> str | None:
        """Return the namespace bound to `(table, columns)`, or None if unbound.

        Unbound columns are not an error at the registry layer: the planner
        only flags missing namespaces for columns that *require* one
        (deterministic-mode columns, FK relationships with no resolvable
        binding). Plain columns with no namespace declaration get None
        and stay namespace-less in the plan.
        """
        for binding in self.bindings:
            for bound_table, bound_cols in binding.declared_by:
                if bound_table == table and bound_cols == columns:
                    return binding.namespace
        return None

    def for_relationship(self, relationship: Relationship) -> str:
        """Resolve the namespace for a FK Relationship; raises if unresolvable.

        Takes the Profile-side `Relationship`, not the graph-side
        `RelationshipEdge` (per S2 spec L1 resolution): callers resolving a
        relationship's namespace haven't built the graph yet (the graph
        consumes the resolved namespace), so the input shape matches what
        callers actually have.
        """
        if relationship.namespace is not None:
            return relationship.namespace
        # Fall back to whatever the parent or child columns are bound to.
        parent_ns = self.for_column(relationship.parent_table, relationship.parent_columns)
        if parent_ns is not None:
            return parent_ns
        child_ns = self.for_column(relationship.child_table, relationship.child_columns)
        if child_ns is not None:
            return child_ns
        raise NamespaceConfigError(
            code="namespace_missing",
            path=(
                f"relationships[{relationship.parent_table}.{relationship.parent_columns}->"
                f"{relationship.child_table}.{relationship.child_columns}]"
            ),
            message=(
                f"Relationship {relationship.parent_table}.{relationship.parent_columns} -> "
                f"{relationship.child_table}.{relationship.child_columns} has no resolvable "
                "namespace. Declare a namespace on the relationship, on the parent column, "
                "or on the child column."
            ),
        )

    def members(self, namespace: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Enumerate every (table, columns) bound to `namespace`."""
        for binding in self.bindings:
            if binding.namespace == namespace:
                return binding.declared_by
        return ()

    def declared(self) -> frozenset[str]:
        """The set of namespaces this registry knows about."""
        return frozenset(b.namespace for b in self.bindings)


def build_namespace_registry(
    config: dict[str, Any],
    profile: Profile,
) -> NamespaceRegistry:
    """Build a `NamespaceRegistry` from config + profile.

    Walks two sources:

    1. `config["namespaces"]`: explicit declarations of the form
       `namespace_name: {declared_by: ["table.col", ...]}`. Each entry
       seeds an initial binding.
    2. `profile.relationships`: FK edges that declare a namespace. The
       child FK column auto-inherits the parent's namespace (the central
       same-FK -> same-mask invariant).

    Raises `NamespaceConfigError` with:

    - `code='namespace_ambiguity'` when the same column is declared in
      two namespaces (explicit declaration vs explicit declaration), OR
      when a child FK column is explicitly bound to a namespace other
      than the namespace it auto-inherits from its parent FK.
    - `code='namespace_missing'` when a deterministic-mode column has
      no namespace declaration (`deterministic: true` requires explicit
      namespace per S1 + S5 R6 reshape).

    Pure function: same `(config, profile)` produces an equal registry.
    """
    # Step 1: parse explicit declarations from config.
    # column_owner: (table, columns) -> namespace name (first declarer).
    # Track ambiguity as the same key landing under two namespaces.
    column_owner: dict[tuple[str, tuple[str, ...]], str] = {}
    namespace_to_columns: dict[str, list[tuple[str, tuple[str, ...]]]] = defaultdict(list)

    namespaces_block = config.get("namespaces", {})
    if isinstance(namespaces_block, dict):
        for ns_name in sorted(namespaces_block.keys()):
            ns_body = namespaces_block[ns_name]
            if not isinstance(ns_body, dict):
                continue
            declared_entries = ns_body.get("declared_by", []) or []
            for entry in declared_entries:
                # Entry format: "table.col" or "table.col1__col2" for composite.
                if not isinstance(entry, str) or "." not in entry:
                    continue
                table, col_part = entry.split(".", 1)
                cols = tuple(col_part.split("__")) if "__" in col_part else (col_part,)
                key = (table, cols)
                if key in column_owner and column_owner[key] != ns_name:
                    _raise_ambiguity(table, cols, sorted({column_owner[key], ns_name}))
                column_owner[key] = ns_name
                if key not in namespace_to_columns[ns_name]:
                    namespace_to_columns[ns_name].append(key)

    # Step 2: auto-bind FK child columns into the parent's namespace.
    # Walks `profile.relationships`. When a relationship declares a
    # namespace, both parent and child columns get bound to it. If the
    # child column is already bound to a different namespace, raise
    # `namespace_ambiguity` (the override-rejection rule from TODO 2).
    for rel in profile.relationships:
        if rel.namespace is None:
            continue
        # Parent side.
        parent_key = (rel.parent_table, rel.parent_columns)
        existing_parent_ns = column_owner.get(parent_key)
        if existing_parent_ns is not None and existing_parent_ns != rel.namespace:
            _raise_ambiguity(
                rel.parent_table,
                rel.parent_columns,
                sorted({existing_parent_ns, rel.namespace}),
            )
        if existing_parent_ns is None:
            column_owner[parent_key] = rel.namespace
            if parent_key not in namespace_to_columns[rel.namespace]:
                namespace_to_columns[rel.namespace].append(parent_key)
        # Child side.
        child_key = (rel.child_table, rel.child_columns)
        existing_child_ns = column_owner.get(child_key)
        if existing_child_ns is not None and existing_child_ns != rel.namespace:
            _raise_ambiguity(
                rel.child_table,
                rel.child_columns,
                sorted({existing_child_ns, rel.namespace}),
            )
        if existing_child_ns is None:
            column_owner[child_key] = rel.namespace
            if child_key not in namespace_to_columns[rel.namespace]:
                namespace_to_columns[rel.namespace].append(child_key)

    # Step 3: deterministic-mode columns must have a namespace.
    tables_block = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables_block:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            # R6 reshape (S5): the deterministic-vs-random axis moved
            # from cardinality_mode='deterministic_map' to a first-class
            # `deterministic: bool` per-column field. The namespace-
            # requirement carries over: deterministic columns must
            # declare a namespace.
            if not bool(col_entry.get("deterministic", False)):
                continue
            col_name = col_entry.get("name", "?")
            key = (table_name, (col_name,))
            explicit_ns = col_entry.get("namespace")
            if explicit_ns:
                # Explicit per-column namespace declaration; treat like a
                # namespaces-block entry. Check for conflict, then register.
                existing = column_owner.get(key)
                if existing is not None and existing != explicit_ns:
                    _raise_ambiguity(table_name, (col_name,), sorted({existing, explicit_ns}))
                if existing is None:
                    column_owner[key] = explicit_ns
                    if key not in namespace_to_columns[explicit_ns]:
                        namespace_to_columns[explicit_ns].append(key)
            elif key not in column_owner:
                raise NamespaceConfigError(
                    code="namespace_missing",
                    path=f"tables.{table_name}.columns.{col_name}",
                    message=(
                        f"Column {table_name}.{col_name} declares "
                        "`deterministic: true` but does not declare a namespace. "
                        "Deterministic columns require an explicit namespace to "
                        "guarantee cross-column consistency."
                    ),
                )

    # Build the final binding tuple, sorted by namespace name for
    # deterministic iteration.
    bindings = tuple(
        NamespaceBinding(namespace=ns, declared_by=tuple(namespace_to_columns[ns]))
        for ns in sorted(namespace_to_columns.keys())
    )
    return NamespaceRegistry(bindings=bindings)


def _raise_ambiguity(table: str, columns: tuple[str, ...], conflicting: list[str]) -> None:
    raise NamespaceConfigError(
        code="namespace_ambiguity",
        path=f"namespaces.{{{','.join(conflicting)}}}.declared_by",
        message=(
            f"Column {table}.{columns} is declared in multiple namespaces: "
            f"{conflicting!r}. Each column may belong to exactly one namespace. "
            "If this column inherits a namespace from a FK relationship, drop "
            "the explicit override; same-FK columns must share a namespace."
        ),
    )
