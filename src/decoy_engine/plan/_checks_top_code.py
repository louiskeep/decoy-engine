"""Plan-compile check for top_code columns (HC-3b).

Added as its own module to avoid growing plan/_checks.py past its size
ceiling (see the SP-10 comment in tests/sentry/test_module_size.py).

`TopCodeStrategyHandler._resolve_top_bound` (`execution/_strategies/
_top_code.py`) returns `None` -- and the handler then raises `StrategyError`
rather than run -- when `preset` names something not in the known preset
table, or when `cap`/`over_label` is missing, non-numeric, or not a non-empty
string. This module rejects the same shapes at compile time, before any row
is masked, so the operator sees the error at plan-compile rather than at
run. It additionally rejects a malformed `floor`/`under_label` pairing, a
`floor >= cap` range, and a `when`-gated top_code column. The handler
(`_resolve_bottom_bound`) also fails closed on a present-but-invalid floor, so
these are caught at compile for the operator's benefit rather than being the
only backstop -- a config that clearly intended a floor and got the shape wrong
should surface at plan-compile, not at run.

Reuses `_PRESETS` from the handler module as the single source of truth for
known preset names (no duplicated table to drift out of sync).

This module exports exactly one function: `check_top_code_config`.
"""

from __future__ import annotations

import math
from typing import Any

from decoy_engine.execution._strategies._top_code import _MAX_EXACT_INT, _PRESETS
from decoy_engine.plan._errors import PlanCompileError


def _reject_unusable_bound(
    value: Any, *, code: str, path: str, kind: str, where: str
) -> int | float:
    """Raise PlanCompileError if `value` cannot serve as an EXACT top_code bound;
    otherwise RETURN it narrowed to `int | float`.

    Returning the validated number (rather than `None`) lets the caller bind
    `cap = _reject_unusable_bound(...)` with an honest `int | float` type instead
    of the `Any | None` a bare `pc.get("cap")` carries -- no cast, mypy-clean.

    Rejects, in order: bool/non-numeric; non-finite (NaN/inf, floats only so a
    huge Python int never raises OverflowError in `math.isfinite`); and
    magnitude >= 2**53 (past float64's exact-integer range, where the tail
    comparison silently rounds and a true tail value can escape generalization --
    a PHI leak Codex reproduced). `where` is the human-readable column locator.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanCompileError(
            code=code,
            path=path,
            message=(
                f"top_code column {where} has invalid {kind} {value!r} "
                f"({type(value).__name__}). Set a numeric {kind} (a numeric-looking "
                "string does not count; the emitter must coerce it to a real number)."
            ),
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise PlanCompileError(
            code=code,
            path=path,
            message=(
                f"top_code column {where} has non-finite {kind} {value!r}. A NaN/inf "
                f"{kind} never fires its tail comparison, so nothing is generalized "
                "and the column passes through unmasked; set a finite numeric bound."
            ),
        )
    if abs(value) >= _MAX_EXACT_INT:
        raise PlanCompileError(
            code=code,
            path=path,
            message=(
                f"top_code column {where} has {kind} {value!r} whose magnitude is at "
                f"or beyond 2**53 ({_MAX_EXACT_INT}). Past that, float64 cannot compare "
                "it exactly against a null-bearing column (Arrow int64+null widens to "
                "float64), so a true tail value can round to the bound and escape "
                f"generalization -- a leak. Use a {kind} with magnitude below 2**53."
            ),
        )
    # Every reject path above raised; the value is a finite in-range number.
    return value


def check_top_code_config(config: dict[str, Any]) -> None:
    """Reject top_code columns whose generalization bound cannot be resolved.

    A column is valid when either:

    1. `preset` is set and is a known preset name (`_PRESETS`); or
    2. `preset` is unset and `cap` is a numeric (`int`/`float`, not `bool`)
       and `over_label` is a non-empty string.

    When `floor` is additionally set (with or without a preset), it must be
    numeric (not `bool`) and paired with a non-empty `under_label`, and the
    effective `floor` must be strictly less than the effective `cap`. A
    `under_label` without a `floor` is rejected (incomplete pair).

    A `nested` column whose child is top_code is rejected outright (top_code is
    not supported as a nested child; see the guard's comment). A `when`-gated
    top_code column is likewise rejected: conditional top-coding skips bound
    validation on a zero match and cannot write its string label back into the
    column's lossless integer dtype (see the guard's comment). Every numeric
    bound (cap, floor) must also have magnitude below 2**53 so the tail
    comparison is exact on a null-bearing column (past that, float64 rounding can
    let a true tail value escape generalization -- a leak).

    Config-only (no profile, no source data): safe to run in both compile
    branches and in `run_config_only_checks`. Validation never mutates
    (per engine rule).

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: the top bound is unresolvable (missing/non-numeric/
            non-finite/>=2**53 cap, or unknown/non-string preset), the bottom
            bound is malformed (bad floor incl. explicit None, floor without
            under_label or vice versa), floor >= cap, top_code is used as a
            nested child, or top_code is combined with `when`.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            col_name = col_entry.get("name", "?")
            pc = col_entry.get("provider_config")
            if not isinstance(pc, dict):
                pc = {}
            where = f"{col_name!r} in table {table_name!r}"

            # Nested (Codex BLOCKER 2/3): a nested column carries its child
            # strategy under provider_config.strategy, so `strategy == "top_code"`
            # never matches it. A top_code nested child is rejected: (a) a per-row
            # format-error RowError is recorded at the FLATTENED-LEAF index, which
            # mis-maps to the wrong OUTER row -- quarantining a clean record while
            # the flagged raw value reaches main output; and (b) the bottom-bound
            # checks below never reach the child config. Fail closed until nested
            # RowError positioning is correct.
            if col_entry.get("strategy") == "nested" and pc.get("strategy") == "top_code":
                raise PlanCompileError(
                    code="top_code_unsupported_in_nested",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.strategy",
                    message=(
                        f"top_code column {where} sets a `nested` strategy with a "
                        "top_code child. top_code is not supported as a nested "
                        "child: a per-row format error is recorded at the "
                        "flattened-leaf position (mis-mapping to the wrong outer "
                        "row, so a flagged raw value can reach main output), and the "
                        "bottom-bound compile checks do not reach the child. Move "
                        "top_code to a top-level column."
                    ),
                )

            if col_entry.get("strategy") != "top_code":
                continue

            # `when` + top_code (Codex R3 HIGH x2): fail closed. Conditional
            # top-coding is unsound on two fronts and neither is worth the
            # machinery for a niche shape. (1) A zero-match `when` skips the
            # handler entirely, so a deserialized/direct Plan with a malformed
            # bound is never validated at run and the column passes through
            # unmasked -- the compile check here is the ONLY backstop, and a
            # gate that the handler can silently skip is not a backstop. (2) The
            # when-gate writes the aggregate label (a str) back into the gated
            # subset; a top_code column ingests as nullable Int64 (lossless FK-
            # safe ingest, so large in-range ints stay exact), and writing "90+"
            # into an Int64 slot raises `ValueError: invalid literal for int()`.
            # top-coding is a whole-column generalization; a partial one is not a
            # supported shape. Mirrors date_shift's `when`+group_by rejection.
            when_val = col_entry.get("when")
            if isinstance(when_val, str) and when_val.strip():
                raise PlanCompileError(
                    code="top_code_with_when_unsupported",
                    path=f"tables.{table_name}.columns.{col_name}.when",
                    message=(
                        f"top_code column {where} combines `when` with top_code. "
                        "Conditional top-coding is not supported: a zero-match "
                        "`when` skips bound validation (unmasked passthrough), and "
                        "the aggregate label cannot be written back into the "
                        "column's lossless integer dtype. top-coding generalizes "
                        "the whole column; remove `when` from this column."
                    ),
                )

            preset = pc.get("preset")
            if preset is not None:
                # `preset` must be a hashable str before the membership test: a
                # list/dict preset would raise `TypeError: unhashable type`.
                if not isinstance(preset, str) or preset not in _PRESETS:
                    raise PlanCompileError(
                        code="top_code_bounds_unresolvable",
                        path=f"tables.{table_name}.columns.{col_name}.provider_config.preset",
                        message=(
                            f"top_code column {where} has preset {preset!r}, which is "
                            f"not a known preset (known: {sorted(_PRESETS)!r}). An "
                            "unresolved preset leaves the column unmasked at run; set "
                            "a known preset or drop 'preset' and set a numeric 'cap' "
                            "+ 'over_label' instead."
                        ),
                    )
                cap: int | float = _PRESETS[preset]["cap"]
            else:
                cap = _reject_unusable_bound(
                    pc.get("cap"),
                    code="top_code_bounds_unresolvable",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.cap",
                    kind="cap",
                    where=where,
                )
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

            # Absence keyed on `"floor" not in pc`, NOT `floor is None`: an
            # explicit `floor: None` is a present-but-malformed bound (operator
            # typo / templating miss), so it must fail closed via
            # `_reject_unusable_bound(None, ...)` below rather than read as "no
            # bottom tail" and silently disable bottom-coding (Codex R3 HIGH --
            # `.get` cannot tell absent from explicit-None). Mirrors the handler.
            if "floor" not in pc:
                # under_label without floor (Codex MEDIUM 3): an incomplete
                # bottom-bound pair silently disables the bottom tail. If the
                # operator supplied under_label they intended a floor; reject
                # rather than silently ignore it.
                if pc.get("under_label") is not None:
                    raise PlanCompileError(
                        code="top_code_under_label_without_floor",
                        path=(
                            f"tables.{table_name}.columns.{col_name}.provider_config.under_label"
                        ),
                        message=(
                            f"top_code column {where} sets 'under_label' but no "
                            "'floor'. The bottom tail is never generalized without a "
                            "floor; set 'floor' or remove 'under_label'."
                        ),
                    )
                continue
            floor = _reject_unusable_bound(
                pc.get("floor"),
                code="top_code_invalid_floor",
                path=f"tables.{table_name}.columns.{col_name}.provider_config.floor",
                kind="floor",
                where=where,
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
