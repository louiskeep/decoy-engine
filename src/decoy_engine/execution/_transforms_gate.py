"""Config-only checks for per-table transforms.

`run_pipeline` owns transforms, so every entry point that masks a table without
going through it (the chunked runners, the native dispatcher, explicit
out-of-core) must refuse a transform-bearing table instead of dropping the ops
silently. These helpers read the config dict only; they never touch a source.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine._lexeme import lexeme_columns_by_table
from decoy_engine.config import post_scan_output_readable, post_scan_resident_reason
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.reasons import ReasonCode

PER_TABLE_TRANSFORMS_PRESENT = reasons.plan.PER_TABLE_TRANSFORMS_PRESENT

__all__ = [
    "PER_TABLE_TRANSFORMS_PRESENT",
    "find_table_config",
    "post_scan_resident_for",
    "reject_any_per_table_transforms",
    "reject_per_table_transforms",
    "transform_bearing_mask_tables",
]


def find_table_config(config: Mapping[str, Any], table: str) -> Mapping[str, Any] | None:
    for entry in config.get("tables") or []:
        if isinstance(entry, Mapping) and entry.get("name") == table:
            return entry
    return None


def _is_transform_bearing_mask(entry: Mapping[str, Any]) -> bool:
    # Generate-kind tables never had transforms applied (generate-side
    # transforms are out of scope), so they are not treated as bearing any.
    return bool(entry.get("transforms")) and not entry.get("generate_columns")


def transform_bearing_mask_tables(config: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(
        str(entry["name"])
        for entry in config.get("tables") or []
        if isinstance(entry, Mapping)
        and isinstance(entry.get("name"), str)
        and _is_transform_bearing_mask(entry)
    )


def reject_per_table_transforms(config: Mapping[str, Any], *, table: str, route: str) -> None:
    """Raise `per_table_transforms_present` when `table` declares transforms.

    `PlanCompileError` is the type the planner already catches to fall back to
    full-frame, so the chunked gate and the planner agree without a new
    exception class.
    """
    entry = find_table_config(config, table)
    if entry is not None and _is_transform_bearing_mask(entry):
        raise PlanCompileError(
            code=PER_TABLE_TRANSFORMS_PRESENT,
            path=f"tables.{table}.transforms",
            message=(
                f"table {table!r} declares transforms; {route} would skip them. "
                "run_pipeline applies transforms over the whole table before any "
                "route reads it, so use it (or apply_table_transforms first)."
            ),
        )


def reject_any_per_table_transforms(config: Mapping[str, Any], *, route: str) -> None:
    """`reject_per_table_transforms` over every transform-bearing mask table."""
    for table in sorted(transform_bearing_mask_tables(config)):
        reject_per_table_transforms(config, table=table, route=route)


def _baseline_readable(
    config: Mapping[str, Any],
    caller_sources: Mapping[str, Any] | None,
    *,
    source_loader_present: bool,
) -> bool:
    """True when the post-scan gate can re-read every input as the run masked it. A loader
    supplies tables nothing can re-read, a lazy transform-bearing table is transformed inside
    the route, and a declared column is cast per chunk by the route; the gate holds none of those
    tables. `caller_sources=None` means residency is not known (an estimate), so it is assumed."""
    if source_loader_present or lexeme_columns_by_table(config):
        return False
    if caller_sources is None:
        return True
    return all(
        isinstance(caller_sources.get(name), pa.Table)
        for name in transform_bearing_mask_tables(config)
    )


def post_scan_resident_for(
    config: Mapping[str, Any],
    *,
    sink_kind: str,
    caller_sources: Mapping[str, Any] | None,
    source_loader_present: bool,
) -> ReasonCode | None:
    """The one place a route's readability facts reach `post_scan_resident_reason`: every
    runtime, compiler and estimator routing site calls this with the facts it holds."""
    return post_scan_resident_reason(
        config,
        output_readable=post_scan_output_readable(sink_kind),
        input_baseline_readable=_baseline_readable(
            config, caller_sources, source_loader_present=source_loader_present
        ),
    )
