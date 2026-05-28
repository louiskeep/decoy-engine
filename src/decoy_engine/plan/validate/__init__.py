"""engine-v2 S10 compile-validation consolidator.

Public API:

    from decoy_engine.plan.validate import (
        PlanCheckError,
        PlanValidationResult,
        validate_plan,
    )

`validate_plan(config, profile, *, decoy_engine_version)` is a thin wrapper over
`compile_plan` that returns a structured `PlanValidationResult` instead of
raising. It runs every compile-time check (rows 1-9 of the ownership table) in
their existing order; S10 adds no check logic. The opt-in post-execution scan
suite lives in `decoy_engine.validation.post`.
"""

from __future__ import annotations

from decoy_engine.plan.validate._consolidator import (
    PlanCheckError,
    PlanValidationResult,
    validate_plan,
)

__all__ = [
    "PlanCheckError",
    "PlanValidationResult",
    "validate_plan",
]
