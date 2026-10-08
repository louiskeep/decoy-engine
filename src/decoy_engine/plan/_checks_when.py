"""Compile-time checks for the per-column `when:` row gate.

Both checks are config-only, so they run in `compile_plan` (both profile modes) and in
`run_config_only_checks`. A column's `when` reaches the engine as a raw dict key that the
pydantic field validator never sees, so the closed-grammar check here is what keeps a
raw-dict caller on the same rule as a validated config. `expressions/_when_parser.parse_when`
stays the only definition of an acceptable predicate.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.errors import ValidationError
from decoy_engine.expressions._when_parser import WHEN_OUTSIDE_GRAMMAR_CODE, parse_when
from decoy_engine.plan._errors import PlanCompileError


def check_when_with_coherent_with(config: dict[str, Any]) -> None:
    """MG-3 / M3 (2026-05-31): reject `when` + `coherent_with` combo
    at compile time with a typed error code.

    The composite generator writes the bundle, not the column. A
    per-column row gate on a coherent_with column is ill-defined:
    skipping the row on one column but not its siblings would
    desynchronize the bundle. The operator sees the typed error and
    can either drop `when` or move the column off the coherent set.
    """
    tables = config.get("tables", []) or []
    for table in tables:
        table_name = table.get("name", "?") if isinstance(table, dict) else "?"
        columns = (table or {}).get("columns", []) if isinstance(table, dict) else []
        for col in columns or []:
            if not isinstance(col, dict):
                continue
            col_name = col.get("name", "?")
            when = col.get("when")
            coherent_with = col.get("coherent_with") or []
            if (
                isinstance(when, str)
                and when.strip()
                and isinstance(coherent_with, (list, tuple))
                and len(coherent_with) > 0
            ):
                raise PlanCompileError(
                    code="when_with_coherent_with_unsupported",
                    path=f"tables.{table_name}.columns.{col_name}.when",
                    message=(
                        f"Column {table_name}.{col_name}: `when:` is not "
                        "supported on columns participating in "
                        "`coherent_with`; the composite generator writes "
                        "the bundle, not the column. Drop `when:` here or "
                        "move the column off the coherent set."
                    ),
                )


def check_when_grammar(config: dict[str, Any]) -> None:
    """Reject a column `when` outside the closed grammar, with the column path.

    A blank or whitespace-only string still means "no gate". A non-string, non-None value
    is rejected too: the seed envelope would otherwise drop it and silently remove a gate
    the caller asked for. The message carries the parser's reason, never the predicate.
    Nested children are not walked: a child's seed is built with `when=None`
    (`_nested.py`), so a `when` key in a child's `strategy_config` is inert provider config.
    """
    for table in config.get("tables", []) or []:
        if not isinstance(table, dict):
            continue
        table_name = table.get("name", "?")
        for col in table.get("columns", []) or []:
            if not isinstance(col, dict):
                continue
            when = col.get("when")
            if when is None:
                continue
            col_name = col.get("name", "?")
            path = f"tables.{table_name}.columns.{col_name}.when"
            if not isinstance(when, str):
                raise PlanCompileError(
                    code=WHEN_OUTSIDE_GRAMMAR_CODE,
                    path=path,
                    message=(
                        f"Column {table_name}.{col_name}: `when:` must be a string "
                        f"predicate in the closed grammar, got {type(when).__name__}."
                    ),
                )
            if not when.strip():
                continue
            try:
                parse_when(when.strip())
                continue
            except ValidationError as exc:
                reason = exc.raw_message
            # Raised outside the handler so `__context__` stays empty.
            raise PlanCompileError(
                code=WHEN_OUTSIDE_GRAMMAR_CODE,
                path=path,
                message=(
                    f"Column {table_name}.{col_name}: {reason}. See the "
                    "`when:` section of docs/strategies.md for the accepted forms."
                ),
            )
