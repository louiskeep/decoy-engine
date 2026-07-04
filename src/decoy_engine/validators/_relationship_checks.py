"""Relationship-aware validators: parent_window_respected, reconciliation_holds
(Sprint 2 honesty pack, S4 / p5-j-validators-extended).

Both validators mirror `_fk_validators.py`'s parent-first edge-lookup pattern
(SDV HMA1, sdv-dev/SDV, MIT): find the declared `relationships:` edge linking
the two named tables, resolve child rows to their parent via the edge's FK
columns, then apply the validator's own check. No second join mechanism is
invented (trap T8); `_edge_for` below is the one lookup helper both share.

parent_window_respected pairs with the `windowed_date` generate strategy:
every child date must fall within its parent's declared window (dbt
relationship-test style, inclusive bounds).

reconciliation_holds pairs with the `derived_aggregate` strategy: a parent
aggregate cell must reconcile with its child rows under an absolute
tolerance (dbt-style relationship/aggregation test; money data legitimately
reconciles to a cent, hence the tolerance knob).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.validators._fk_validators import _child_fk_values, _extract_relationships
from decoy_engine.validators._types import ValidatorFinding


def _edge_for(
    config: dict[str, Any], *, parent_table: str, child_table: str, validator_name: str
) -> dict[str, Any]:
    """Return the unique relationship edge linking parent_table -> child_table.

    Raises ValueError (fail loud, D2's no-silent-skip rule) when zero or more
    than one edge matches: an ambiguous or missing edge must not silently
    produce zero findings.
    """
    matches = []
    for rel in _extract_relationships(config):
        parent_info = rel.get("parent") or {}
        if parent_info.get("table") != parent_table:
            continue
        children = rel.get("children") or []
        if not isinstance(children, list):
            continue
        if any(isinstance(c, dict) and c.get("table") == child_table for c in children):
            matches.append(rel)
    if len(matches) != 1:
        raise ValueError(
            f"{validator_name}: found {len(matches)} relationship edge(s) linking "
            f"parent {parent_table!r} -> child {child_table!r} (expected exactly 1). "
            "Declare exactly one relationships: entry for this pair."
        )
    return matches[0]


def _require_params(params: dict[str, Any], keys: list[str], validator_name: str) -> None:
    missing = [k for k in keys if not params.get(k)]
    if missing:
        raise ValueError(
            f"{validator_name} validator requires params {missing!r} (all are required strings)."
        )


# ---------------------------------------------------------------------------
# parent_window_respected
# ---------------------------------------------------------------------------


def validate_parent_window_respected(
    outputs: dict[str, pa.Table],
    entry: dict[str, Any],
    config: dict[str, Any],
    *,
    sources: dict[str, pa.Table] | None = None,
) -> tuple[ValidatorFinding, ...]:
    """parent_window_respected: every child date falls within its parent's window.

    Bounds are inclusive on both ends. A non-null child date that fails to
    parse IS a finding (a generated date that does not parse is itself a
    defect); null child dates are skipped. A null or unparseable parent
    window bound makes every child row mapped to that parent a finding (the
    window cannot be verified: fail loud, D2's no-silent-skip rule), not a
    skip.

    Args:
        outputs: Read-only pipeline outputs.
        entry: Config block; requires `params.child_table`, `child_column`,
            `parent_table`, `window_start_column`, `window_end_column`.
        config: Full pipeline config dict; the relationship edge is read
            from `config["relationships"]`.
        sources: Unused by this validator (registry contract, D2).

    Returns:
        Tuple of ValidatorFinding (at most one, for the child table).

    Raises:
        ValueError: Missing params, or zero/multiple matching relationship
            edges for (parent_table, child_table).
    """
    params: dict[str, Any] = entry.get("params") or {}
    _require_params(
        params,
        ["child_table", "child_column", "parent_table", "window_start_column", "window_end_column"],
        "parent_window_respected",
    )
    child_table_name: str = params["child_table"]
    child_column: str = params["child_column"]
    parent_table_name: str = params["parent_table"]
    start_col: str = params["window_start_column"]
    end_col: str = params["window_end_column"]

    edge = _edge_for(
        config,
        parent_table=parent_table_name,
        child_table=child_table_name,
        validator_name="parent_window_respected",
    )
    parent_cols: list[str] = (edge.get("parent") or {}).get("columns") or []
    child_info = next(
        c
        for c in edge.get("children") or []
        if isinstance(c, dict) and c.get("table") == child_table_name
    )
    child_fk_cols: list[str] = child_info.get("columns") or []

    parent_table = outputs.get(parent_table_name)
    if parent_table is None:
        raise ValueError(
            f"parent_window_respected: parent table {parent_table_name!r} not found in outputs."
        )
    child_table = outputs.get(child_table_name)
    if child_table is None or child_column not in child_table.schema.names:
        return ()

    # Build parent PK tuple -> (window_start, window_end) as pandas Timestamps
    # (NaT when null/unparseable, which is the fail-loud signal below).
    parent_pk_lists = [parent_table.column(c).to_pylist() for c in parent_cols]
    starts = pd.to_datetime(pd.Series(parent_table.column(start_col).to_pylist()), errors="coerce")
    ends = pd.to_datetime(pd.Series(parent_table.column(end_col).to_pylist()), errors="coerce")
    window_by_key: dict[tuple[Any, ...], tuple[pd.Timestamp, pd.Timestamp]] = {}
    for i in range(parent_table.num_rows):
        key = tuple(col[i] for col in parent_pk_lists)
        if any(v is None for v in key):
            continue
        window_by_key[key] = (starts.iloc[i], ends.iloc[i])

    child_fk_values = _child_fk_values(child_table, child_fk_cols)
    child_dates = pd.to_datetime(
        pd.Series(child_table.column(child_column).to_pylist()), errors="coerce"
    )
    child_raw = child_table.column(child_column).to_pylist()

    failing: list[int] = []
    for i, fk in enumerate(child_fk_values):
        if fk is None:
            continue  # null FK: not this validator's concern (see no_orphan_children)
        if child_raw[i] is None:
            continue  # null child date: skipped, not a violation
        if pd.isna(child_dates.iloc[i]):
            failing.append(i)  # non-null child date that fails to parse: a defect
            continue
        window = window_by_key.get(fk)
        if window is None:
            continue  # FK does not resolve to a parent row: fk_intact's concern, not this one
        start, end = window
        if pd.isna(start) or pd.isna(end):
            failing.append(i)  # window cannot be verified: fail loud, not skip
            continue
        if not (start <= child_dates.iloc[i] <= end):
            failing.append(i)

    if not failing:
        return ()
    return (
        ValidatorFinding(
            validator="parent_window_respected",
            table=child_table_name,
            column=child_column,
            failing_row_indices=tuple(failing),
            detail=(
                f"{len(failing)} row(s) in {child_table_name}.{child_column} fall outside "
                f"the parent {parent_table_name!r} window ({start_col}..{end_col}), fail to "
                "parse, or map to an unverifiable (null/unparseable) parent window"
            ),
        ),
    )


# ---------------------------------------------------------------------------
# reconciliation_holds
# ---------------------------------------------------------------------------

_OPS = frozenset({"sum", "count"})


def validate_reconciliation_holds(
    outputs: dict[str, pa.Table],
    entry: dict[str, Any],
    config: dict[str, Any],
    *,
    sources: dict[str, pa.Table] | None = None,
) -> tuple[ValidatorFinding, ...]:
    """reconciliation_holds: a parent aggregate reconciles with its child rows.

    Groups child rows by the FK edge, applies `op` ("sum" default, or
    "count"), and compares the result to the parent cell with an absolute
    tolerance (`math.isclose`-style, default 1e-6). Non-numeric parent or
    child cells are findings, not skips (a reconciliation that cannot be
    computed is itself a defect).

    Finding granularity: one finding per parent table, `failing_row_indices`
    are the PARENT rows whose reconciliation failed (the parent aggregate is
    the asserted fact; quarantining the parent row removes the inconsistent
    aggregate from the output). `detail` carries the worst absolute delta
    observed.

    Args:
        outputs: Read-only pipeline outputs.
        entry: Config block; requires `params.parent_table`, `parent_column`,
            `child_table`, `child_column`; optional `op` (default "sum") and
            `tolerance` (default 1e-6).
        config: Full pipeline config dict; the relationship edge is read
            from `config["relationships"]`.
        sources: Unused by this validator (registry contract, D2).

    Returns:
        Tuple of ValidatorFinding (at most one, for the parent table).

    Raises:
        ValueError: Missing params, unknown `op`, or zero/multiple matching
            relationship edges for (parent_table, child_table).
    """
    params: dict[str, Any] = entry.get("params") or {}
    _require_params(
        params,
        ["parent_table", "parent_column", "child_table", "child_column"],
        "reconciliation_holds",
    )
    parent_table_name: str = params["parent_table"]
    parent_column: str = params["parent_column"]
    child_table_name: str = params["child_table"]
    child_column: str = params["child_column"]
    op: str = str(params.get("op", "sum"))
    tolerance: float = float(params.get("tolerance", 1e-6))
    if op not in _OPS:
        raise ValueError(f"reconciliation_holds: unknown op {op!r}. Known ops: {sorted(_OPS)}.")

    edge = _edge_for(
        config,
        parent_table=parent_table_name,
        child_table=child_table_name,
        validator_name="reconciliation_holds",
    )
    parent_pk_cols: list[str] = (edge.get("parent") or {}).get("columns") or []
    child_info = next(
        c
        for c in edge.get("children") or []
        if isinstance(c, dict) and c.get("table") == child_table_name
    )
    child_fk_cols: list[str] = child_info.get("columns") or []

    parent_table = outputs.get(parent_table_name)
    if parent_table is None:
        raise ValueError(
            f"reconciliation_holds: parent table {parent_table_name!r} not found in outputs."
        )
    child_table = outputs.get(child_table_name)
    if child_table is None:
        return ()
    if (
        parent_column not in parent_table.schema.names
        or child_column not in child_table.schema.names
    ):
        return ()

    child_fk_values = _child_fk_values(child_table, child_fk_cols)
    child_raw = child_table.column(child_column).to_pylist()

    # Group child rows by FK key. "count": group size (row count). "sum":
    # numeric sum; a non-numeric non-null cell makes the whole group
    # unreconcilable (recorded as a NaN sentinel so the parent-side compare
    # below turns it into a finding rather than silently skipping it).
    group_agg: dict[tuple[Any, ...], float] = {}
    group_bad: set[tuple[Any, ...]] = set()
    for i, fk in enumerate(child_fk_values):
        if fk is None:
            continue
        if op == "count":
            group_agg[fk] = group_agg.get(fk, 0.0) + 1.0
            continue
        val = child_raw[i]
        if val is None:
            continue
        try:
            numeric = float(val)
        except (TypeError, ValueError):
            group_bad.add(fk)
            continue
        group_agg[fk] = group_agg.get(fk, 0.0) + numeric

    parent_pk_lists = [parent_table.column(c).to_pylist() for c in parent_pk_cols]
    parent_values = parent_table.column(parent_column).to_pylist()

    failing: list[int] = []
    worst_delta = 0.0
    for i in range(parent_table.num_rows):
        key = tuple(col[i] for col in parent_pk_lists)
        parent_val = parent_values[i]
        if key in group_bad:
            failing.append(i)
            continue
        try:
            parent_numeric = float(parent_val) if parent_val is not None else None
        except (TypeError, ValueError):
            parent_numeric = None
        if parent_numeric is None:
            failing.append(i)
            continue
        agg = group_agg.get(key, 0.0)
        delta = abs(parent_numeric - agg)
        if delta > tolerance:
            failing.append(i)
            worst_delta = max(worst_delta, delta)

    if not failing:
        return ()
    return (
        ValidatorFinding(
            validator="reconciliation_holds",
            table=parent_table_name,
            column=parent_column,
            failing_row_indices=tuple(failing),
            detail=(
                f"{len(failing)} row(s) in {parent_table_name}.{parent_column} do not "
                f"reconcile with {child_table_name}.{child_column} ({op}, tolerance={tolerance}); "
                f"worst absolute delta observed: {worst_delta}"
            ),
        ),
    )
