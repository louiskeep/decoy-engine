"""Routing-signal rules for jobs that declare per-table transforms.

Routing has to price the data that will run. A resident transform-bearing table is
prepared (transformed) before routing, so the byte estimate and the probe see its
real row count and fields, derived columns included and dropped ones gone. A table
that is not resident cannot be prepared without reading it, so its transformed
size is unknowable at admission:

- Prepared jobs get the ordinary byte-estimate routing on the prepared size. The
  probe child receives the prepared tables and a config copy with their transforms
  cleared, so nothing is transformed twice.
- A lazy transform-bearing table is never priced or probed; under `auto` the job
  never reaches full-frame (`decide_execution_route` applies that rule whatever the
  byte-estimate flag says).
- Out-of-core never runs a transform-bearing job (it streams raw sources), so the
  auto route declines it and the cause is surfaced as telemetry.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution._transforms_gate import (
    PER_TABLE_TRANSFORMS_PRESENT,
    find_table_config,
    transform_bearing_mask_tables,
)
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "admission_signals",
    "check_rejections_before_preparation",
    "decline_out_of_core",
    "out_of_core_declined",
    "routing_profile",
    "stamp_out_of_core_declined",
]

_ROW_CHANGING_OPS = frozenset({"filter", "limit", "dedupe"})


def _estimate_in_scope(profile: Any, table_kinds: dict[str, str]) -> bool:
    kinds = set(table_kinds.values())
    return bool(getattr(profile, "relationships", None)) and kinds == {"mask"}


def check_rejections_before_preparation(
    config: Mapping[str, Any],
    caller_sources: Mapping[str, pa.Table | LazySource],
    profile: Any,
    graph: RelationshipGraph,
    *,
    execution_mode: str,
    has_generate_table: bool,
    has_mask_table: bool,
    validators: list[Any],
    fidelity_report: bool,
    vault_writer: Any,
    post_validation: bool,
    resolved_substrate: str,
) -> None:
    """Raise what routing would raise for this job, from the profile, graph and the
    residency of the sources alone, so a job that is going to be rejected never
    prepares (transforms) a table: an explicit `sequential` request that cannot run,
    or an `auto` relationship job with a lazy transform-bearing mask table that has
    no bounded route."""
    bearing = transform_bearing_mask_tables(config)
    if not bearing or execution_mode not in ("sequential", "auto"):
        return
    from decoy_engine.execution._pipeline_routing import (
        _has_cross_table_fk_cycle,
        _sequential_eligible,
        lazy_transform_route,
        reject_explicit_sequential,
    )

    eligible, route_reason = _sequential_eligible(
        profile,
        has_generate_table=has_generate_table,
        validators=validators,
        fidelity_report=fidelity_report,
        vault_writer=vault_writer,
        post_validation=post_validation,
        resolved_substrate=resolved_substrate,
    )
    cyclic = _has_cross_table_fk_cycle(graph)
    if execution_mode == "sequential":
        reject_explicit_sequential(eligible, route_reason, cyclic, has_mask_table)
        return
    lazy = any(not isinstance(caller_sources.get(name), pa.Table) for name in bearing)
    if lazy and profile.relationships and has_mask_table:
        lazy_transform_route(eligible, route_reason, cyclic)


def decline_out_of_core(
    config: Mapping[str, Any], compatible: bool, reject_code: str | None
) -> tuple[bool, str | None]:
    """Out-of-core streams raw sources and cannot apply a table's transforms."""
    if transform_bearing_mask_tables(config):
        return False, PER_TABLE_TRANSFORMS_PRESENT
    return compatible, reject_code


def routing_profile(
    profile: Any, sources: Mapping[str, pa.Table | LazySource], prepared: frozenset[str]
) -> Any:
    """`profile` with each prepared table's row count replaced by the prepared count.

    The count of a prepared table is authoritative (filter, limit and dedupe make
    it differ from the raw profile by design), so routing sizes and the row-count
    reconciliation see one consistent number and nothing warns about the change.
    """
    if not prepared:
        return profile
    tables = tuple(
        dataclasses.replace(t, row_count=sources[t.name].num_rows, row_count_exact=True)
        if t.name in prepared
        else t
        for t in profile.tables
    )
    return dataclasses.replace(profile, tables=tables)


def _specs(
    mask_tables: list[Any], sources: Mapping[str, pa.Table | LazySource], prepared: frozenset[str]
) -> tuple[Any, ...]:
    from decoy_engine.execution._mem_estimate_schema import (
        table_size_spec_from_profile,
        table_size_spec_from_table,
    )
    from decoy_engine.execution._pipeline_routing_signals import _resident_column_arrays

    return tuple(
        table_size_spec_from_table(t.name, sources[t.name])
        if t.name in prepared
        else table_size_spec_from_profile(t, sample=_resident_column_arrays(sources.get(t.name), t))
        for t in mask_tables
    )


def _prepared_estimate(
    profile: Any,
    sources: Mapping[str, pa.Table | LazySource],
    table_kinds: dict[str, str],
    prepared: frozenset[str],
    budget_bytes: int,
) -> bool | None:
    from decoy_engine.execution._mem_estimate import fits

    mask_tables = [t for t in profile.tables if table_kinds.get(t.name) == "mask"]
    if not mask_tables:
        return None
    return fits(_specs(mask_tables, sources, prepared), "full_frame", budget_bytes)


def _row_changing(config: Mapping[str, Any], name: str) -> bool:
    entry = find_table_config(config, name) or {}
    return any(op.get("op") in _ROW_CHANGING_OPS for op in entry.get("transforms") or [])


def _derived_names(config: Mapping[str, Any], name: str) -> set[str]:
    """Columns bound to a derived value after the table's ops (a dropped-then-derived
    name is derived; a derived-then-dropped name is gone)."""
    derived: set[str] = set()
    for op in (find_table_config(config, name) or {}).get("transforms") or []:
        if op.get("op") == "derive":
            derived.add(op["column"])
        elif op.get("op") == "drop_column":
            derived -= set(op.get("columns") or [])
    return derived


def _distinct_counts(
    config: Mapping[str, Any],
    mask_tables: list[Any],
    sources: Mapping[str, pa.Table | LazySource],
    prepared: frozenset[str],
) -> dict[tuple[str, str], int] | None:
    """Per-column distinct counts for the uniqueness-saturation guard, or `None` when
    a measurement fails (the probe is then inconclusive).

    A prepared table is read from its own fields: derived columns are always measured
    (the raw profile never saw them), dropped columns are absent, and every column is
    measured when a row-changing op made the raw counts unusable. Only an untouched
    source column of a row-preserving table reuses its raw profile count."""
    out: dict[tuple[str, str], int] = {}
    for t in mask_tables:
        table = sources[t.name]
        raw = {c.name: c.distinct_count for c in t.columns if c.distinct_count is not None}
        if t.name not in prepared or not isinstance(table, pa.Table):
            out.update({(t.name, col): n for col, n in raw.items()})
            continue
        measure_all = _row_changing(config, t.name)
        derived = _derived_names(config, t.name)
        for col in table.column_names:
            if col not in derived and not measure_all:
                if col in raw:
                    out[(t.name, col)] = raw[col]
                continue
            try:
                out[(t.name, col)] = int(pc.count_distinct(table.column(col)).as_py())  # type: ignore[attr-defined]
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError):
                return None
    return out


def _prepared_probe(
    config: dict[str, Any],
    profile: Any,
    sources: Mapping[str, pa.Table | LazySource],
    table_kinds: dict[str, str],
    prepared: frozenset[str],
    budget_arg: int | None,
    engine_version: str,
    error_band: float = 0.30,
) -> bool | None:
    """The B2 probe recovery for a job whose transform-bearing tables are all prepared.

    Same rules as `resolve_probe_recovery`, but sized and run on the prepared
    tables: their row counts pick the reference table and target, and the child
    gets a config copy whose prepared tables have their transforms cleared.
    """
    mask_tables = [t for t in profile.tables if table_kinds.get(t.name) == "mask"]
    if not mask_tables or any(
        t.name not in sources or isinstance(sources[t.name], LazySource) for t in mask_tables
    ):
        return None
    from decoy_engine.execution._mem_estimate import raw_data_bytes
    from decoy_engine.execution._probe import (
        DEFAULT_PROBE_TIMEOUT_S,
        MIN_PLAUSIBLE_K_FULL_FRAME,
        probe_fits,
        probe_peak_bytes,
        uniqueness_saturation_risk,
    )
    from decoy_engine.execution.out_of_core import resolve_budget

    budget = resolve_budget(budget_arg)
    raw = raw_data_bytes(_specs(mask_tables, sources, prepared))
    if raw.priceable_bytes * MIN_PLAUSIBLE_K_FULL_FRAME > budget.budget_bytes:
        return None
    # `routing_profile` already carries the prepared row counts.
    counts = {t.name: t.row_count for t in mask_tables}
    distinct = _distinct_counts(config, mask_tables, sources, prepared)
    if distinct is None:
        return None
    largest = max(mask_tables, key=lambda t: t.row_count)
    probe_config = copy.deepcopy(config)
    for entry in probe_config["tables"]:
        if entry["name"] in prepared:
            entry["transforms"] = []
    resident = {n: t for n, t in sources.items() if isinstance(t, pa.Table)}
    result = probe_peak_bytes(
        probe_config,
        resident,
        reference_table=largest.name,
        target_rows=largest.row_count,
        uniqueness_risk_columns=uniqueness_saturation_risk(counts, distinct),
        raw_floor_bytes=raw.priceable_bytes if raw.is_priceable else None,
        mem_cap_bytes=budget.budget_bytes,
        timeout_s=DEFAULT_PROBE_TIMEOUT_S,
        engine_version=engine_version,
    )
    return probe_fits(result, budget.budget_bytes, error_band=error_band)


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
    prepared_tables: frozenset[str] = frozenset(),
) -> tuple[bool | None, bool | None, bool]:
    """`(full_frame_fits_estimate, probe_recovers_full_frame, lazy_transform_bearing)`.

    `profile` is the routing profile (`routing_profile`). Jobs without transforms
    take the unchanged estimate and probe. When every transform-bearing table is
    prepared the estimate and probe run on the prepared tables. When any is lazy
    both are `None` (the router's lazy rule then keeps the job off full-frame).
    The signals are only read for relationship-bearing pure-mask jobs under `auto`,
    so every other transform-bearing shape skips both and spawns no probe.
    """
    from decoy_engine.execution._pipeline_routing_signals import (
        resolve_full_frame_fits_estimate,
        resolve_probe_recovery,
    )

    bearing = transform_bearing_mask_tables(config)
    lazy = bool(bearing - prepared_tables)
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
        return fits, probe, False
    if (
        lazy
        or execution_mode != "auto"
        or not use_byte_estimate_routing
        or not _estimate_in_scope(profile, table_kinds)
    ):
        return None, None, lazy
    from decoy_engine.execution.out_of_core import resolve_budget

    budget = resolve_budget(out_of_core_budget_bytes).budget_bytes
    fits = _prepared_estimate(profile, caller_sources, table_kinds, prepared_tables, budget)
    probe = None
    if use_probe_routing and fits is not True:
        probe = _prepared_probe(
            config,
            profile,
            caller_sources,
            table_kinds,
            prepared_tables,
            out_of_core_budget_bytes,
            engine_version,
        )
    return fits, probe, False


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
