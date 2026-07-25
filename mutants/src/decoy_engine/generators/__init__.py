"""
Data generation module for the decoy_engine package.

S9: the V1 ``DataGenerator`` + ``RelationshipHandler`` entry points were
removed from the public surface and the underlying modules deleted.
``ColumnGenerator`` stays: V2 ``generation.synthesize`` delegates to it for
parity-frozen formula + cardinality bounds (Reading B pragmatic parity), and
``graph/ops/generate_op`` uses it for the graph-mode generate node.
"""

from decoy_engine.generators.columns import ColumnGenerator

__all__ = ["ColumnGenerator"]
