"""Plan-compile check for grouped_series columns (SP-10c).

Added as its own module to avoid growing plan/_checks.py past its size
ceiling. See the SP-10 comment in tests/sentry/test_module_size.py: the
_checks.py module decomposes into per-strategy sub-modules as new strategies
land; this is the SP-10c slice for grouped_series.

This module exports exactly one function: ``check_grouped_series_refs``,
which validates that grouped_series columns name existing group_by and
order_by columns. It is imported by plan/_compile.py alongside the other
check functions from plan/_checks.py and sibling _checks_*.py modules.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.grouped_series import GROUPED_SERIES_GENERATORS


def check_grouped_series_refs(config: dict[str, Any]) -> None:
    """Reject grouped_series columns with invalid generator or missing column refs.

    Compile-check ownership table row #18 (SP-10c / P5.S.grouped_series.1,
    2026-06-29). Three failure modes caught here (plan-compile time, before any
    execution):

    1. Invalid generator: the ``generator`` key must be in GROUPED_SERIES_GENERATORS
       (the closed enumeration). An unknown generator is guaranteed to raise at
       execution time; surfacing it here gives a precise error before any run.

    2. Missing group_by ref: the ``group_by`` key names a column not present
       in the same table. A missing ref is guaranteed to raise KeyError at
       execution time; rejecting here surfaces it with the exact missing name.

    3. Missing order_by ref: the ``order_by`` key names a column not present
       in the same table. Same rationale.

    Covers both mask-kind columns (strategy: grouped_series with
    provider_config.group_by / provider_config.order_by) and generate-kind
    columns (type: grouped_series with group_by / order_by at the column
    level).

    Config-only (no profile, no source data): safe to run in both compile
    branches and in ``run_config_only_checks``. Validation never mutates
    (per engine rule).

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: A group_by or order_by column ref is missing from
            the same table.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")

        # Build the full set of column names known in this table.
        all_col_names: set[str] = set()
        for col_entry in table_entry.get("columns", []) or []:
            if isinstance(col_entry, dict) and col_entry.get("name"):
                all_col_names.add(str(col_entry["name"]))
        for col_entry in table_entry.get("generate_columns", []) or []:
            if isinstance(col_entry, dict) and col_entry.get("name"):
                all_col_names.add(str(col_entry["name"]))

        # Check mask-kind columns (strategy: grouped_series).
        for col_entry in table_entry.get("columns") or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "grouped_series":
                continue
            col_name = col_entry.get("name", "?")
            pc = col_entry.get("provider_config") or {}
            if not isinstance(pc, dict):
                continue
            generator = pc.get("generator")
            if generator and str(generator) not in GROUPED_SERIES_GENERATORS:
                raise PlanCompileError(
                    code="grouped_series_generator_invalid",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.generator",
                    message=(
                        f"grouped_series column {col_name!r} in table "
                        f"{table_name!r} has invalid generator {generator!r}. "
                        f"Allowed values: {sorted(GROUPED_SERIES_GENERATORS)!r}."
                    ),
                )
            _check_ref(
                col_ref=pc.get("group_by"),
                ref_key="group_by",
                col_name=col_name,
                table_name=table_name,
                all_col_names=all_col_names,
                path_prefix=f"tables.{table_name}.columns.{col_name}.provider_config",
            )
            _check_ref(
                col_ref=pc.get("order_by"),
                ref_key="order_by",
                col_name=col_name,
                table_name=table_name,
                all_col_names=all_col_names,
                path_prefix=f"tables.{table_name}.columns.{col_name}.provider_config",
            )

        # Check generate-kind columns (type: grouped_series).
        for col_entry in table_entry.get("generate_columns") or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("type") != "grouped_series":
                continue
            col_name = col_entry.get("name", "?")
            generator = col_entry.get("generator")
            if generator and str(generator) not in GROUPED_SERIES_GENERATORS:
                raise PlanCompileError(
                    code="grouped_series_generator_invalid",
                    path=f"tables.{table_name}.generate_columns.{col_name}.generator",
                    message=(
                        f"grouped_series column {col_name!r} in table "
                        f"{table_name!r} has invalid generator {generator!r}. "
                        f"Allowed values: {sorted(GROUPED_SERIES_GENERATORS)!r}."
                    ),
                )
            _check_ref(
                col_ref=col_entry.get("group_by"),
                ref_key="group_by",
                col_name=col_name,
                table_name=table_name,
                all_col_names=all_col_names,
                path_prefix=f"tables.{table_name}.generate_columns.{col_name}",
            )
            _check_ref(
                col_ref=col_entry.get("order_by"),
                ref_key="order_by",
                col_name=col_name,
                table_name=table_name,
                all_col_names=all_col_names,
                path_prefix=f"tables.{table_name}.generate_columns.{col_name}",
            )


def _check_ref(
    col_ref: Any,
    ref_key: str,
    col_name: str,
    table_name: str,
    all_col_names: set[str],
    path_prefix: str,
) -> None:
    """Raise PlanCompileError when col_ref is set but not in all_col_names."""
    if col_ref and str(col_ref) not in all_col_names:
        raise PlanCompileError(
            code=f"grouped_series_missing_{ref_key}_ref",
            path=f"{path_prefix}.{ref_key}",
            message=(
                f"grouped_series column {col_name!r} in table {table_name!r} "
                f"references {ref_key} column {col_ref!r} which is not defined "
                f"in the same table. "
                f"Available columns: {sorted(all_col_names)!r}."
            ),
        )
