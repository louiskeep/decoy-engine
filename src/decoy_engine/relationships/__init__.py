"""Relationship coordinator + namespace registry: the engine-v2 S2 module.

S1 sketched the relationship + namespace plumbing inside the planner (the
`Relationship` dataclass in `Profile`, the `namespaces:` + `relationships:`
+ `ordering:` blocks in `Plan`, the namespace-ambiguity and
fk-plan-ordering compile checks). S2 promotes that plumbing to a
first-class engine module: this package.

Public API:

    from decoy_engine.relationships import (
        # Graph
        OrphanPolicy,
        RelationshipEdge,
        RelationshipGraph,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
        # Namespace registry
        NamespaceBinding,
        NamespaceConfigError,
        NamespaceRegistry,
        build_namespace_registry,
    )

The two builders compose: `build_namespace_registry` runs first (raises on
ambiguity), then `check_orphan_fk_policy_completeness` validates orphan
policies and returns a lookup, then `build_relationship_graph` consumes
both and produces the resolved `RelationshipGraph`.

Source patterns documented in each submodule header. DAG construction
draws from networkx + dbt's manifest dependency model (immutable artifact,
sorted-queue Kahn's algorithm). Namespace registry draws from URN-style
lookups in language servers (LSP textDocument URI -> capabilities) and
package managers (canonical name -> metadata).
"""

from __future__ import annotations

from decoy_engine.relationships._graph import (
    OrphanPolicy,
    RelationshipEdge,
    RelationshipGraph,
    build_relationship_graph,
    check_orphan_fk_policy_completeness,
)
from decoy_engine.relationships._namespace import (
    NamespaceBinding,
    NamespaceConfigError,
    NamespaceRegistry,
    build_namespace_registry,
)

__all__ = [
    "NamespaceBinding",
    "NamespaceConfigError",
    "NamespaceRegistry",
    "OrphanPolicy",
    "RelationshipEdge",
    "RelationshipGraph",
    "build_namespace_registry",
    "build_relationship_graph",
    "check_orphan_fk_policy_completeness",
]
