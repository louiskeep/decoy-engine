"""Routing-signal rules for jobs that declare per-table transforms.

Routing runs on the raw profile, before transforms. Row counts stay a safe upper
bound (filter, limit and dedupe only shrink; sort, drop and derive keep them),
but `derive` widens rows and pandas or numexpr can promote narrow inputs (an int8
expression can yield int64 or float64), so no raw-profile byte formula bounds a
transformed job. Two rules follow:

- A transform-bearing job is never admitted to full-frame by the static byte
  estimate. Only the measured probe can recover it, and the probe runs the real
  transformed config in its child process.
- Out-of-core never runs a transform-bearing job (it would have to skip the ops),
  so the auto route declines it and the cause is surfaced as telemetry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._transforms_gate import (
    PER_TABLE_TRANSFORMS_PRESENT,
    transform_bearing_mask_tables,
)
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

__all__ = ["admission_signals", "out_of_core_declined", "stamp_out_of_core_declined"]


def _estimate_in_scope(profile: Any, table_kinds: dict[str, str]) -> bool:
    kinds = set(table_kinds.values())
    return (
        bool(getattr(profile, "relationships", None))
        and "mask" in kinds
        and "generate" not in kinds
    )


def admission_signals(
    config: dict[str, Any],
    *,
    profile: Any,
    caller_sources: dict[str, pa.Table | LazySource],
    table_kinds: dict[str, str],
    execution_mode: str,
    use_byte_estimate_routing: bool,
    use_probe_routing: bool,
    out_of_core_budget_bytes: int | None,
    engine_version: str,
) -> tuple[bool | None, bool | None]:
    """`(full_frame_fits_estimate, probe_recovers_full_frame)` for the router.

    Jobs without transforms take the unchanged estimate and probe. A
    transform-bearing job never gets a static fit (`None`, unpriceable). The
    estimate and probe are only read for relationship-bearing pure-mask jobs
    under `auto`, so every other transform-bearing shape skips both: no probe
    subprocess is spawned for a result nothing consumes, and a source whose field
    names cannot be looked up unambiguously is left to the transform guard to
    reject with a coded error.
    """
    from decoy_engine.execution._pipeline_routing_signals import (
        resolve_full_frame_fits_estimate,
        resolve_probe_recovery,
    )

    bearing = transform_bearing_mask_tables(config)
    if not bearing:
        fits = resolve_full_frame_fits_estimate(
            use_byte_estimate_routing,
            profile,
            caller_sources,
            table_kinds,
            out_of_core_budget_bytes,
        )
        probe = resolve_probe_recovery(
            use_probe_routing,
            use_byte_estimate_routing,
            profile,
            caller_sources,
            table_kinds,
            out_of_core_budget_bytes,
            fits,
            config=config,
            engine_version=engine_version,
        )
        return fits, probe
    ambiguous = any(
        isinstance(src, pa.Table) and len(set(src.schema.names)) != len(src.schema.names)
        for name, src in caller_sources.items()
        if name in bearing
    )
    if execution_mode != "auto" or ambiguous or not _estimate_in_scope(profile, table_kinds):
        return None, None
    probe = resolve_probe_recovery(
        use_probe_routing,
        use_byte_estimate_routing,
        profile,
        caller_sources,
        table_kinds,
        out_of_core_budget_bytes,
        None,
        config=config,
        engine_version=engine_version,
    )
    return None, probe


def out_of_core_declined(
    config: dict[str, Any],
    *,
    plan: Plan,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    profile: Any,
    table_kinds: dict[str, str],
    execution_mode: str,
) -> str | None:
    """`per_table_transforms_present` when transforms are the reason an `auto` job
    skipped out-of-core: the job is a relationship-bearing pure-mask job whose
    plan the out-of-core compatibility gate would otherwise have admitted."""
    if execution_mode != "auto" or not transform_bearing_mask_tables(config):
        return None
    if not _estimate_in_scope(profile, table_kinds):
        return None
    from decoy_engine.execution._pipeline_routing_signals import out_of_core_admission

    compatible, _ = out_of_core_admission(plan, registry=registry, graph=graph)
    return PER_TABLE_TRANSFORMS_PRESENT if compatible else None


def stamp_out_of_core_declined(quality_metrics: dict[str, Any], reason: str | None) -> None:
    """Record the decline under `quality_metrics["execution"]`; no key when `None`,
    so every telemetry shape without a decline is unchanged."""
    if reason is not None:
        quality_metrics["execution"]["out_of_core_declined"] = reason
