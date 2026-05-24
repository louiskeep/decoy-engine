"""Focused graph-validator modules (V2.0-B).

Replaces the bundled `GraphConfigValidator` class in
`decoy_engine.internal.validator` with independently-testable pure
functions. Each module owns one stage of validation:

  - top_level: schema_version, mode, nodes/edges shape, engine flag
  - nodes:     per-node id, kind, name, NATIVE_ENGINE, config delegation
  - edges:     edge shape, cardinality, acyclic topology
  - cross_node: file-format consistency (lenient warning), mask
                column reachability, nodes-ref reachability
  - FK / m2m / multi-parent: already extracted to
    `decoy_engine.graph._fk_validators` in V2.0-A

The done-state contract is "validation never mutates input." None of
these functions write to the caller's dict. Normalization (e.g. format
back-fill on target.file) lives in `decoy_engine.graph.normalize` and
runs only when callers ask for it.
"""

from __future__ import annotations

from decoy_engine.graph.validators.cross_node import (
    validate_file_format_consistency,
    validate_mask_column_reachability,
    validate_nodes_ref_reachability,
)
from decoy_engine.graph.validators.edges import (
    validate_acyclic,
    validate_cardinality,
    validate_edges,
)
from decoy_engine.graph.validators.nodes import (
    collect_node_errors,
    validate_nodes,
)
from decoy_engine.graph.validators.top_level import (
    known_kinds,
    validate_top_level,
)

__all__ = [
    "collect_node_errors",
    "known_kinds",
    "validate_acyclic",
    "validate_cardinality",
    "validate_edges",
    "validate_file_format_consistency",
    "validate_mask_column_reachability",
    "validate_nodes",
    "validate_nodes_ref_reachability",
    "validate_top_level",
]
