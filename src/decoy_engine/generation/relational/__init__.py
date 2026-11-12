"""Cross-table generation: the shapes a child column takes from its parent table.

`_cardinality` holds the reference-pool check every non-fitted route calls and the per-parent
bounds repair. Callers import the names from this package.
"""

from __future__ import annotations

from decoy_engine.generation.relational._cardinality import (
    apply_cardinality_bounds,
    check_fitted_bounds,
    check_reference_pool,
    reference_policies,
)

__all__ = [
    "apply_cardinality_bounds",
    "check_fitted_bounds",
    "check_reference_pool",
    "reference_policies",
]
