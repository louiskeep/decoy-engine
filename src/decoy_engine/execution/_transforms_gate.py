"""Config-only checks for per-table transforms.

`run_pipeline` owns transforms, so every entry point that masks a table without
going through it (the chunked runners, the native dispatcher, explicit
out-of-core) must refuse a transform-bearing table instead of dropping the ops
silently. These helpers read the config dict only; they never touch a source.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from decoy_engine.plan._errors import PlanCompileError

PER_TABLE_TRANSFORMS_PRESENT = "per_table_transforms_present"

__all__ = [
    "PER_TABLE_TRANSFORMS_PRESENT",
    "find_table_config",
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
