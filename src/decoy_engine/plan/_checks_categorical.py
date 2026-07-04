"""Plan-compile check for categorical (mask) columns (Sprint 13 / coercion-13, S3).

Added as its own module to avoid growing plan/_checks.py past its size
ceiling (see the SP-10 comment in tests/sentry/test_module_size.py).

Sprint 13 GATE-1 Q4 (PO-approved, 2026-07-03): close the sibling silent-
corruption leak in the same pass as truncate. `CategoricalStrategyHandler.run`
(`execution/_strategies/_categorical.py`) and its polars twin
(`execution/polars/_strategies/_categorical.py`) both do
``categories = list(cfg.get("categories", []))``. When ``categories`` is a
plain string (e.g. the Studio picker's free-text field emitted with no
coercion), ``list(...)`` iterates its CHARACTERS rather than raising or
treating it as one category: the output silently becomes single characters
resampled from the string, not the configured category set. This module
rejects that shape at compile time. The handlers additionally raise
`StrategyError` on the same shape as a defense-in-depth backstop.

`from_profile: true` is exempted (matches the Sprint 13 guide D5): when set,
the authoring layer is responsible for resolving real categories from the
column's profile before the engine ever sees the config; the engine does not
perform that resolution itself, so it must not reject a from_profile column
for having no explicit categories.

This module exports exactly one function: `check_categorical_categories`.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def check_categorical_categories(config: dict[str, Any]) -> None:
    """Reject categorical (mask) columns whose categories cannot be parsed as a list.

    Compile-check ownership table row #24 (Sprint 13 / coercion-13 S3,
    GATE-1 Q4, 2026-07-03). Two failure modes, both config-only:

    1. `categories` is present but not a list/tuple (most notably a plain
       string): `list("gold,silver")` iterates characters, silently
       corrupting the output instead of raising or masking with the
       intended category set. Raises `categorical_categories_not_list`.
    2. `categories` is missing, `None`, or an empty list/tuple: the handler
       already raises `StrategyError(categorical_requires_categories)` at
       execution time; surfacing it at compile time gives the same signal
       before a run starts. Raises `categorical_categories_missing`.

    Neither check applies when `from_profile` is truthy: that flag signals
    the authoring layer resolves real categories from the profile before
    the engine sees the config (Sprint 13 guide D5); the engine performs no
    such resolution itself, so it must not reject a from_profile column for
    lacking explicit categories.

    Config-only (no profile, no source data): safe to run in both compile
    branches and in `run_config_only_checks`. Validation never mutates
    (per engine rule).

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: `categories` is not a proper non-empty list and
            `from_profile` is not set.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "categorical":
                continue
            col_name = col_entry.get("name", "?")
            pc = col_entry.get("provider_config")
            if not isinstance(pc, dict):
                pc = {}

            if pc.get("from_profile"):
                continue  # authoring layer resolves categories; engine does not

            categories = pc.get("categories")

            if categories is not None and not isinstance(categories, (list, tuple)):
                raise PlanCompileError(
                    code="categorical_categories_not_list",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.categories",
                    message=(
                        f"categorical column {col_name!r} in table {table_name!r} has "
                        f"categories={categories!r} ({type(categories).__name__}), which "
                        "is not a list. A string value iterates as individual "
                        "characters at runtime, silently corrupting the output "
                        "instead of masking with the intended category set. "
                        "Provide categories as a list, e.g. "
                        "['gold', 'silver', 'bronze']."
                    ),
                )

            if categories is None or len(categories) == 0:
                raise PlanCompileError(
                    code="categorical_categories_missing",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.categories",
                    message=(
                        f"categorical column {col_name!r} in table {table_name!r} has "
                        "no categories. Provide a non-empty 'categories' list, or set "
                        "'from_profile: true' to derive categories from the source "
                        "column's profile."
                    ),
                )
