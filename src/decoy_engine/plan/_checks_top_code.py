"""Plan-compile check for top_code columns (HC-3b).

Added as its own module to avoid growing plan/_checks.py past its size
ceiling (see the SP-10 comment in tests/sentry/test_module_size.py).

`TopCodeStrategyHandler._resolve_top_bound` (`execution/_strategies/
_top_code.py`) returns `None` -- and the handler then raises `StrategyError`
rather than run -- when `preset` names something not in the known preset
table, or when `cap`/`over_label` is missing, non-numeric, or not a non-empty
string. This module rejects the same shapes at compile time, before any row
is masked, so the operator sees the error at plan-compile rather than at
run. It additionally rejects a malformed `floor`/`under_label` pairing and a
`floor >= cap` range, both of which the handler silently treats as "no
floor" rather than raising (the bottom tail is optional, so degrading is
safe at the handler layer, but a config that clearly intended a floor and
got the shape wrong should be caught here).

Reuses `_PRESETS` from the handler module as the single source of truth for
known preset names (no duplicated table to drift out of sync).

This module exports exactly one function: `check_top_code_config`.
"""

from __future__ import annotations

import math
from typing import Any

from decoy_engine.execution._strategies._top_code import _PRESETS
from decoy_engine.plan._errors import PlanCompileError


def check_top_code_config(config: dict[str, Any]) -> None:
    """Reject top_code columns whose generalization bound cannot be resolved.

    A column is valid when either:

    1. `preset` is set and is a known preset name (`_PRESETS`); or
    2. `preset` is unset and `cap` is a numeric (`int`/`float`, not `bool`)
       and `over_label` is a non-empty string.

    When `floor` is additionally set (with or without a preset), it must be
    numeric (not `bool`) and paired with a non-empty `under_label`, and the
    effective `floor` must be strictly less than the effective `cap`.

    Config-only (no profile, no source data): safe to run in both compile
    branches and in `run_config_only_checks`. Validation never mutates
    (per engine rule).

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: the top bound is unresolvable, the bottom bound is
            malformed, or floor >= cap.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "top_code":
                continue
            col_name = col_entry.get("name", "?")
            pc = col_entry.get("provider_config")
            if not isinstance(pc, dict):
                pc = {}

            preset = pc.get("preset")
            if preset is not None:
                if preset not in _PRESETS:
                    raise PlanCompileError(
                        code="top_code_bounds_unresolvable",
                        path=f"tables.{table_name}.columns.{col_name}.provider_config.preset",
                        message=(
                            f"top_code column {col_name!r} in table {table_name!r} "
                            f"has preset {preset!r}, which is not a known preset "
                            f"(known: {sorted(_PRESETS)!r}). An unresolved preset "
                            "leaves the column unmasked at run; set a known preset "
                            "or drop 'preset' and set a numeric 'cap' + 'over_label' "
                            "instead."
                        ),
                    )
                cap: int | float = _PRESETS[preset]["cap"]
            else:
                raw_cap = pc.get("cap")
                if isinstance(raw_cap, bool) or not isinstance(raw_cap, (int, float)):
                    raise PlanCompileError(
                        code="top_code_bounds_unresolvable",
                        path=f"tables.{table_name}.columns.{col_name}.provider_config.cap",
                        message=(
                            f"top_code column {col_name!r} in table {table_name!r} "
                            f"has invalid cap {raw_cap!r} "
                            f"({type(raw_cap).__name__}). Set either a known "
                            "'preset' or a numeric 'cap' (a numeric-looking "
                            "string does not count; the emitter must coerce it "
                            "to a real number before it reaches the engine)."
                        ),
                    )
                if not math.isfinite(raw_cap):
                    raise PlanCompileError(
                        code="top_code_bounds_unresolvable",
                        path=f"tables.{table_name}.columns.{col_name}.provider_config.cap",
                        message=(
                            f"top_code column {col_name!r} in table {table_name!r} "
                            f"has non-finite cap {raw_cap!r}. A NaN/inf cap never "
                            "fires the `value > cap` comparison, so nothing is "
                            "generalized and the column passes through unmasked; "
                            "set a finite numeric cap."
                        ),
                    )
                cap = raw_cap
                over_label = pc.get("over_label")
                if not isinstance(over_label, str) or not over_label:
                    raise PlanCompileError(
                        code="top_code_missing_over_label",
                        path=(f"tables.{table_name}.columns.{col_name}.provider_config.over_label"),
                        message=(
                            f"top_code column {col_name!r} in table "
                            f"{table_name!r} sets a numeric cap but has no "
                            f"non-empty 'over_label' (got {over_label!r}). Set "
                            "'over_label' to the aggregate label for values "
                            'above cap (e.g. "90+").'
                        ),
                    )

            floor = pc.get("floor")
            if floor is None:
                continue
            if isinstance(floor, bool) or not isinstance(floor, (int, float)):
                raise PlanCompileError(
                    code="top_code_invalid_floor",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.floor",
                    message=(
                        f"top_code column {col_name!r} in table {table_name!r} "
                        f"has invalid floor {floor!r} ({type(floor).__name__}). "
                        "'floor' must be a numeric (int or float), not a bool "
                        "or a string."
                    ),
                )
            if not math.isfinite(floor):
                raise PlanCompileError(
                    code="top_code_invalid_floor",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.floor",
                    message=(
                        f"top_code column {col_name!r} in table {table_name!r} "
                        f"has non-finite floor {floor!r}. A NaN/inf floor never "
                        "fires the `value < floor` comparison, so the bottom tail "
                        "is never generalized; set a finite numeric floor."
                    ),
                )
            under_label = pc.get("under_label")
            if not isinstance(under_label, str) or not under_label:
                raise PlanCompileError(
                    code="top_code_missing_under_label",
                    path=(f"tables.{table_name}.columns.{col_name}.provider_config.under_label"),
                    message=(
                        f"top_code column {col_name!r} in table {table_name!r} "
                        f"sets 'floor' but has no non-empty 'under_label' "
                        f"(got {under_label!r}). Set 'under_label' to the "
                        "aggregate label for values below floor."
                    ),
                )
            if floor >= cap:
                raise PlanCompileError(
                    code="top_code_floor_ge_cap",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.floor",
                    message=(
                        f"top_code column {col_name!r} in table {table_name!r} "
                        f"has floor {floor!r} >= cap {cap!r}. floor must be "
                        "strictly less than cap, or every value falls into "
                        "one tail or the other with nothing left in range."
                    ),
                )
