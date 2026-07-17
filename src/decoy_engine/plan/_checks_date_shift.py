"""Plan-compile check for date_shift `group_by` columns (HC-3a).

Added as its own module to avoid growing plan/_checks.py past its size
ceiling. See the SP-10 comment in tests/sentry/test_module_size.py: the
_checks.py module decomposes into per-strategy sub-modules as new strategies
land; this is the HC-3a slice for date_shift.

This module exports exactly one function: ``check_date_shift_group_by_refs``,
which validates that date_shift columns using ``group_by`` name an existing
entity column. It is imported by plan/_compile.py alongside the other check
functions from plan/_checks.py and sibling _checks_*.py modules.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def check_date_shift_group_by_refs(config: dict[str, Any]) -> None:
    """Reject malformed / unsupported date_shift `group_by` refs.

    Compile-check ownership table row #27 (HC-3a, 2026-07-17). Failure modes
    caught here (plan-compile time, before any execution):

    1. Missing group_by ref: the ``group_by`` key (in ``provider_config``)
       names a column not present in the same table. A missing ref is
       guaranteed to raise KeyError at execution time; rejecting here surfaces
       it with the exact missing name.
    2. Non-string group_by ref (Codex R1 P2 #3): ``group_by: 123`` would slip
       past a bare ``str(group_by) in cols`` membership test if a column named
       "123" existed, but execution does ``df[123]`` -> KeyError. A group_by
       ref must be a non-empty string.
    3. group_by inside a ``nested`` child (Codex R1 P2 #4): a ``nested``
       strategy whose scalar child is ``date_shift`` with a ``group_by`` is
       rejected. The nested child runs against a synthetic single-column
       ``_nested_leaves`` frame with no sibling columns, so the entity anchor
       can never be present -- fail closed rather than KeyError at execution.
    5. group_by on an FK-participating column (Codex R2 P1s): rejected. Entity
       anchoring makes the shift depend on a sibling value, so equal FK keys can
       mask to different dates -- breaking RI on the chunked FK self-masking
       route and mis-anchoring orphan REMAP. date_shift WITHOUT group_by is
       FK-safe (per-value deterministic); only the combination is rejected.
    4. group_by combined with ``when`` (Codex R1 P1 #1 residual): rejected. The
       pre-mask anchor is label-aligned to the frame the handler sees, which is
       correct on pandas but not on the polars-native when-gate (it filters to a
       fresh RangeIndex), so the combination would silently mis-anchor on the
       polars route. Fail closed until per-route positional anchoring lands.

    date_shift is mask-kind only (no generate-kind `type: date_shift`), so
    unlike windowed_date/group_key the top-level check has a single loop.

    Config-only (no profile, no source data): safe to run in both compile
    branches and in ``run_config_only_checks``. Validation never mutates
    (per engine rule).

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: A group_by ref is missing, non-string, or nested.
    """
    # FK-participating (table, column) pairs (Codex R2 P1s). A date_shift with
    # `group_by` makes the shift depend on a per-entity anchor, which BREAKS the
    # invariant every FK route relies on -- that equal source key -> equal masked
    # key. On the chunked FK self-masking route an FK child so configured can
    # shift differently from its parent (RI break); under orphan_policy=remap an
    # FK parent so configured is invoked on a synthetic orphan-only frame whose
    # RangeIndex mis-aligns the anchor (or whose snapshot was already evicted in
    # sequential mode). date_shift WITHOUT group_by stays FK-safe (per-value
    # deterministic), so only the group_by combination is rejected.
    fk_cols: set[tuple[str, str]] = set()
    for rel in config.get("relationships", []) or []:
        if not isinstance(rel, dict):
            continue
        ends = [rel.get("parent"), *(rel.get("children") or [])]
        for end in ends:
            if not isinstance(end, dict):
                continue
            end_table = end.get("table")
            for end_col in end.get("columns", []) or []:
                if end_table and end_col:
                    fk_cols.add((str(end_table), str(end_col)))

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")

        # Build the set of column names known in this table. date_shift is
        # mask-kind only, so a group_by anchor must be a real (masked/source)
        # `columns` entry present in the frame at mask time -- deliberately NOT
        # unioning `generate_columns` (unlike windowed_date, which can anchor on
        # a generate-side column). Including them would let a group_by naming a
        # generate-only column pass compile and then KeyError at execution;
        # restricting to `columns` lets compile fully own the validation.
        all_col_names: set[str] = set()
        for col_entry in table_entry.get("columns", []) or []:
            if isinstance(col_entry, dict) and col_entry.get("name"):
                all_col_names.add(str(col_entry["name"]))

        for col_entry in table_entry.get("columns") or []:
            if not isinstance(col_entry, dict):
                continue
            col_name = col_entry.get("name", "?")
            pc = col_entry.get("provider_config") or {}
            if not isinstance(pc, dict):
                continue

            # Nested (P2 #4): a nested column carries its child strategy +
            # config under provider_config.strategy / .strategy_config, so
            # top-level `strategy == "date_shift"` never matches it. Inspect it
            # explicitly and reject a nested date_shift+group_by before the
            # generic branch below.
            if col_entry.get("strategy") == "nested" and pc.get("strategy") == "date_shift":
                nested_cfg = pc.get("strategy_config") or {}
                if isinstance(nested_cfg, dict) and nested_cfg.get("group_by"):
                    raise PlanCompileError(
                        code="date_shift_group_by_unsupported_in_nested",
                        path=(
                            f"tables.{table_name}.columns.{col_name}"
                            ".provider_config.strategy_config.group_by"
                        ),
                        message=(
                            f"date_shift column {col_name!r} in table "
                            f"{table_name!r} sets group_by inside a `nested` "
                            "strategy. group_by is not supported there: the "
                            "nested child masks a synthetic single-column leaf "
                            "batch with no sibling columns, so the entity anchor "
                            "can never be present. Move date_shift+group_by to a "
                            "top-level column."
                        ),
                    )
                continue

            if col_entry.get("strategy") != "date_shift":
                continue
            group_by = pc.get("group_by")
            # Falsy (None, "", 0, []) means "no group_by": the handler's own
            # `if group_by:` gate skips it, so compile agrees and does not error.
            if not group_by:
                continue
            # Non-string but truthy (P2 #3): e.g. `group_by: 123` or a list.
            # `str(123) in cols` would slip past a bare membership test if a
            # column named "123" existed, but execution does `df[123]` ->
            # KeyError. Reject before the membership test.
            if not isinstance(group_by, str):
                raise PlanCompileError(
                    code="date_shift_missing_group_by_ref",
                    path=(f"tables.{table_name}.columns.{col_name}.provider_config.group_by"),
                    message=(
                        f"date_shift column {col_name!r} in table "
                        f"{table_name!r} has a group_by that is not a string "
                        f"(got {group_by!r}). group_by must name a column in the "
                        "same table."
                    ),
                )
            if group_by not in all_col_names:
                raise PlanCompileError(
                    code="date_shift_missing_group_by_ref",
                    path=(f"tables.{table_name}.columns.{col_name}.provider_config.group_by"),
                    message=(
                        f"date_shift column {col_name!r} in table "
                        f"{table_name!r} references group_by column "
                        f"{group_by!r} which is not defined in the same "
                        f"table. Available columns: {sorted(all_col_names)!r}."
                    ),
                )
            # FK column + group_by (Codex R2 P1s): fail closed. A group_by shift
            # depends on a per-entity anchor, so equal FK keys can mask to
            # different dates -- breaking referential integrity on the chunked FK
            # self-masking route (FK child) and mis-anchoring orphan REMAP (FK
            # parent). Reject until per-route FK-anchor threading exists.
            if (table_name, col_name) in fk_cols:
                raise PlanCompileError(
                    code="date_shift_group_by_on_fk_column_unsupported",
                    path=(f"tables.{table_name}.columns.{col_name}.provider_config.group_by"),
                    message=(
                        f"date_shift column {col_name!r} in table {table_name!r} "
                        "participates in a foreign-key relationship AND sets "
                        "group_by. Entity-anchored shifting makes the masked value "
                        "depend on a sibling anchor, which breaks referential "
                        "integrity (equal keys can shift apart) and orphan remap. "
                        "Remove group_by from this FK column, or drop the FK "
                        "relationship on it."
                    ),
                )
            # `when` + `group_by` (Codex R1 P1 #1 residual): fail closed. The
            # entity anchor is aligned to the frame the handler sees by index
            # LABEL, which is correct on every pandas route (a when-gated subset
            # keeps its parent-table labels). But the polars-native when-gate
            # filters to a FRESH RangeIndex (the original positions survive only
            # in the gate's internal `_decoy_when_row_pos` anchor), so a
            # label-reindex there silently picks the WRONG rows -- a wrong-output
            # hole the handler cannot detect (both indexes are RangeIndexes).
            # Rather than ship a route-dependent silent-wrong-output, reject the
            # combination until per-route positional anchoring is implemented.
            # date_shift's core per-entity-consistent shift does not need `when`.
            if col_entry.get("when"):
                raise PlanCompileError(
                    code="date_shift_group_by_with_when_unsupported",
                    path=(f"tables.{table_name}.columns.{col_name}.when"),
                    message=(
                        f"date_shift column {col_name!r} in table "
                        f"{table_name!r} combines `when` with `group_by`. This "
                        "combination is not yet supported: on the polars route a "
                        "when-gated subset cannot be re-aligned to the pre-mask "
                        "entity anchor without producing wrong offsets. Remove "
                        "`when` or `group_by` from this column."
                    ),
                )
