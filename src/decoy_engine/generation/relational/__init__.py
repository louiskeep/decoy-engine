"""Cross-table generation: the shapes a child column takes from its parent table.

`_cardinality` holds the reference-pool check every non-fitted route calls, the per-parent
bounds repair and the extra arguments a reference draw is called with. Callers import the
names from this package.
"""

from __future__ import annotations

from decoy_engine.generation.relational._cardinality import (
    apply_cardinality_bounds,
    check_fitted_bounds,
    check_reference_pool,
    reference_draw_extras,
    reference_policies,
)

__all__ = [
    "apply_cardinality_bounds",
    "check_fitted_bounds",
    "check_reference_pool",
    "reference_draw_extras",
    "reference_policies",
]
