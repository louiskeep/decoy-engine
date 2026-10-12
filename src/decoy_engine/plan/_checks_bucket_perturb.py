"""Plan-compile check for bucket_perturb columns whose `date_format` cannot write a date back.

bucket_perturb parses each value with `date_format` and writes the perturbed
date back with the same format. A format with no date directive (`ISO8601`,
`mixed`, `foo`) makes `strftime` return its own text, so every parsed date was
silently replaced by that text. The rule lives in
`transforms.bucket_perturb.bucket_perturb_date_format_problem`; the handlers and
the native gate apply the same rule as a backstop. Same compile-plus-backstop
shape as `_checks_truncate`.

Exports exactly one function: ``check_bucket_perturb_config``. It also rejects an invalid
``preserve_values`` (D1c) so the error lands at compile time, not on the first row.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.bucket_perturb import bucket_perturb_date_format_problem
from decoy_engine.transforms.date_preserve import preserve_config_problem


def _reject_if_unwritable(
    provider_config: Any, *, column: str, table: str, path_prefix: str, where: str
) -> None:
    if not isinstance(provider_config, dict):
        return
    if "date_format" in provider_config:
        problem = bucket_perturb_date_format_problem(provider_config["date_format"])
        if problem is not None:
            raise PlanCompileError(
                code="bucket_perturb_date_format_unsupported",
                path=f"{path_prefix}.date_format",
                message=f"bucket_perturb column {column!r} in table {table!r}{where}: {problem}",
            )
    # D1c: a malformed or unusable `preserve_values` is a config error even when no row is
    # read (a zero-row table or a `when:` that selects nothing).
    problem = preserve_config_problem(provider_config)
    if problem is not None:
        raise PlanCompileError(
            code="bucket_perturb_preserve_values_invalid",
            path=f"{path_prefix}.preserve_values",
            message=f"bucket_perturb column {column!r} in table {table!r}{where}: {problem}",
        )


def check_bucket_perturb_config(config: dict[str, Any]) -> None:
    """Reject bucket_perturb columns (and nested bucket_perturb children) whose
    `date_format` has no date directive, an unknown directive or a dangling `%`, or whose
    `preserve_values` is malformed or lacks a usable `date_format`.

    Config-only (no profile, no source data): safe in both compile branches and
    in ``run_config_only_checks``. Validation never mutates.

    Raises:
        PlanCompileError: ``bucket_perturb_date_format_unsupported`` or
            ``bucket_perturb_preserve_values_invalid``.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            strategy = col_entry.get("strategy")
            if strategy not in ("bucket_perturb", "nested"):
                continue
            col_name = col_entry.get("name", "?")
            pc = col_entry.get("provider_config")
            prefix = f"tables.{table_name}.columns.{col_name}.provider_config"
            if strategy == "bucket_perturb":
                _reject_if_unwritable(
                    pc, column=col_name, table=table_name, path_prefix=prefix, where=""
                )
            elif isinstance(pc, dict) and pc.get("strategy") == "bucket_perturb":
                _reject_if_unwritable(
                    pc.get("strategy_config"),
                    column=col_name,
                    table=table_name,
                    path_prefix=f"{prefix}.strategy_config",
                    where=" (nested child)",
                )
