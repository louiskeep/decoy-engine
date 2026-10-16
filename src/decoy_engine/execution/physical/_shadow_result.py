"""`ShadowRunResult`: the staged result of one shadow run.

Lives in its own module so the coordinator (dispatch) and the batch runner
(per-node loop) can both build it without importing each other at runtime.
`_shadow_coordinator` re-exports it, so existing importers are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pyarrow as pa

    from decoy_engine.execution.physical._context import SeamContext
    from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence

__all__ = ["ShadowRunResult"]


@dataclass(frozen=True)
class ShadowRunResult:
    """The staged (never published) result of one shadow run: the masked
    output tables, per-node route evidence keyed by `node_id`, and
    diagnostics. `warnings`/`row_errors` mirror `ExecutionResult`'s shape for
    multiset comparison. Slice 5b-ii's FK resolution can populate `warnings`;
    native date_shift populates `row_errors` with table-attributed,
    table-global `RowErrorRecord`s (its format_error rows).

    `driver_invocation` (slice 3, widened by slice 6) is the `SeamContext`
    the OUT_OF_CORE or FULL_FRAME branch recorded (`None` elsewhere -- the
    per-node loop has no single adapter call to name); `route_evidence`
    stays empty on both (no per-node loop runs there). `quality_metrics`
    (slice 4, widened by slice 6) carries `ExecutionResult.quality_metrics`
    through by reference for those same branches; the per-node loop below
    produces no metric-bearing strategy in this slice, so it keeps the
    empty default.
    """

    outputs: dict[str, pa.Table]
    route_evidence: dict[str, OperatorCallEvidence]
    warnings: tuple[object, ...] = ()
    row_errors: tuple[object, ...] = ()
    driver_invocation: SeamContext | None = None
    quality_metrics: dict[str, Any] = field(default_factory=dict)
