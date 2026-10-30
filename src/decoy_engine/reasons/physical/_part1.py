"""Catalog of the ``physical`` namespace, part 1."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "CELL_VALUE_DIFF",
    "DIAGNOSTICS_DIFF",
    "DUPLICATE_NODE_DECLARATION",
    "FAKER_POOL_NON_STRING_OUTPUT",
    "FULL_FRAME_DISPATCH_MISSING_DEPENDENCY",
    "FULL_FRAME_DRIVER_UNSUPPORTED",
    "FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED",
    "FULL_FRAME_SUBSTRATE_UNSUPPORTED",
    "GENERATION_SHAPE_UNSUPPORTED",
    "GLOBAL_SHUFFLE_DETERMINISM_UNSUPPORTED",
    "GLOBAL_STRATEGY_UNSUPPORTED",
    "MIXED_DRIVER_UNSUPPORTED",
    "MIXED_FK_UNADMITTED_CHILD",
    "NATIVE_COMPANION_UNAVAILABLE",
    "NULL_MASK_DIFF",
    "OOC_DISPATCH_MISSING_DEPENDENCY",
    "OOC_FK_PARITY_DIFF",
    "OPERATOR_INVARIANT_VIOLATION",
    "OPERATOR_NOT_EXECUTED",
    "PLANNED_VS_ACTUAL_ROUTE_DIFF",
    "PUBLICATION_ATTEMPT",
    "RESOURCE_LIMIT_BREACH",
    "ROW_COUNT_DIFF",
    "ROW_ORDER_DIFF",
    "SCHEMA_DIFF",
    "SNAPSHOT_IDENTITY_DIFF",
]

_d = _catalog.physical("ShadowDifference")

DUPLICATE_NODE_DECLARATION = _d("duplicate-node-declaration", "Duplicate node declaration.")
FAKER_POOL_NON_STRING_OUTPUT = _d("faker-pool-non-string-output", "Faker pool non string output.")
FULL_FRAME_DISPATCH_MISSING_DEPENDENCY = _d(
    "full-frame-dispatch-missing-dependency", "Full frame dispatch missing dependency."
)
FULL_FRAME_DRIVER_UNSUPPORTED = _d(
    "full-frame-driver-unsupported", "Full frame driver unsupported."
)
FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED = _d(
    "full-frame-runtime-feature-unsupported", "Full frame runtime feature unsupported."
)
FULL_FRAME_SUBSTRATE_UNSUPPORTED = _d(
    "full-frame-substrate-unsupported", "Full frame substrate unsupported."
)
GENERATION_SHAPE_UNSUPPORTED = _d("generation-shape-unsupported", "Generation shape unsupported.")
GLOBAL_SHUFFLE_DETERMINISM_UNSUPPORTED = _d(
    "global-shuffle-determinism-unsupported", "Global shuffle determinism unsupported."
)
GLOBAL_STRATEGY_UNSUPPORTED = _d("global-strategy-unsupported", "Global strategy unsupported.")
MIXED_DRIVER_UNSUPPORTED = _d("mixed-driver-unsupported", "Mixed driver unsupported.")
MIXED_FK_UNADMITTED_CHILD = _d("mixed-fk-unadmitted-child", "Mixed FK unadmitted child.")
NATIVE_COMPANION_UNAVAILABLE = _d("native_companion_unavailable", "Native companion unavailable.")
OOC_DISPATCH_MISSING_DEPENDENCY = _d(
    "ooc-dispatch-missing-dependency", "Out-of-core dispatch missing dependency."
)
OPERATOR_INVARIANT_VIOLATION = _d("operator-invariant-violation", "Operator invariant violation.")
OPERATOR_NOT_EXECUTED = _d("operator-not-executed", "Operator not executed.")
PLANNED_VS_ACTUAL_ROUTE_DIFF = _d("planned-vs-actual-route-diff", "Planned vs actual route diff.")
RESOURCE_LIMIT_BREACH = _d("resource-limit-breach", "Resource limit breach.")
SCHEMA_DIFF = _d("schema-diff", "Schema diff.")

_d = _catalog.physical("decoy_engine.execution.physical._shadow_diff_codes")

CELL_VALUE_DIFF = _d("cell-value-diff", "Cell value diff.")
DIAGNOSTICS_DIFF = _d("diagnostics-diff", "Diagnostics diff.")
NULL_MASK_DIFF = _d("null-mask-diff", "Null mask diff.")
OOC_FK_PARITY_DIFF = _d("ooc-fk-parity-diff", "Out-of-core FK parity diff.")
PUBLICATION_ATTEMPT = _d("publication-attempt", "Publication attempt.")
ROW_COUNT_DIFF = _d("row-count-diff", "Row count diff.")
ROW_ORDER_DIFF = _d("row-order-diff", "Row order diff.")
SNAPSHOT_IDENTITY_DIFF = _d("snapshot-identity-diff", "Snapshot identity diff.")
