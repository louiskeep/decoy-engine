"""Native columnar-streaming execution package (engine-efficiency program).

Phase 0 landed evidence and contracts only. Phases 1-2 built the compiled
kernels, the config/type-aware eligibility query, and (Task 2.7, this module's
re-export) the dispatch that routes an admitted table's masking through them
inside the Phase 1 streaming coordinator. ``_determinism_protocol`` still holds
the machine-checkable RNG draw-site inventory the whole package is built to
preserve exactly.
"""

from __future__ import annotations

from decoy_engine.execution.native._dispatch import (
    NativeRouteEvidence,
    NodeRouteRecord,
    plan_native_route,
    run_native_or_oracle_chunked,
)

__all__ = [
    "NativeRouteEvidence",
    "NodeRouteRecord",
    "plan_native_route",
    "run_native_or_oracle_chunked",
]
