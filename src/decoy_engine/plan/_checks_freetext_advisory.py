"""Plan-compile check: clinical free-text advisory (HC-7).

Own module for the same `_checks.py` / `_compile.py` size-ceiling reason
as the sibling per-strategy check modules (see `_checks_top_code.py`,
`_checks_date_shift.py`, ...). Unlike those, this check never raises --
it is warn-only by design (see `quality/_freetext_advisory.py` for the
full rationale) -- so it returns warning strings for the caller to fold
into `PlanCompileResult.warnings`, the same shape as
`check_fpe_join_groups`.

`check_freetext_advisory` is the single chokepoint that turns
(config, profile) into the `FreetextColumnView` list the pure scorer
needs: it walks every MASK table's columns, keeps only explicit
`strategy: passthrough` columns (a column with any other strategy is
already handled; an unconfigured column never reaches here as a
`ColumnConfig` at all -- that case is warned separately by
`execution/_output_projection.py`, and this check does not duplicate it),
and looks up each one's `avg_length` / `distinct_count` / dtype from the
matching `ColumnProfile` when the profile has one. A column with no
matching profile entry (missing profile, or a profile taken before this
field existed) degrades gracefully: `dtype_known_non_string=False` and
`avg_length=None`, so only the name-hint branch can fire for it -- see
`FreetextColumnView`'s docstring for why that default is False, not True.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.profile._types import ColumnProfile, Profile
from decoy_engine.quality._freetext_advisory import (
    FreetextColumnView,
    freetext_advisory_min_avg_length,
    freetext_advisory_min_distinctness,
    is_string_dtype_label,
    score_unmasked_freetext,
)


def check_freetext_advisory(config: dict[str, Any], profile: Profile) -> tuple[str, ...]:
    """Return one advisory warning per unmasked likely-clinical-free-text
    column. Never raises; the config and profile are read-only inputs and
    are not mutated.
    """
    profile_columns = _index_profile_columns(profile)
    min_avg_length = freetext_advisory_min_avg_length(config)
    min_distinctness = freetext_advisory_min_distinctness(config)

    views: list[FreetextColumnView] = []
    for table in config.get("tables", []) or []:
        if not isinstance(table, dict):
            continue
        table_name = table.get("name", "?")
        for col in table.get("columns", []) or []:
            if not isinstance(col, dict):
                continue
            if col.get("strategy") != "passthrough":
                continue
            col_name = col.get("name", "?")
            profile_col = profile_columns.get((table_name, col_name))
            if profile_col is None:
                views.append(
                    FreetextColumnView(
                        name=col_name,
                        strategy="passthrough",
                        dtype_known_non_string=False,
                        avg_length=None,
                        distinct_count=None,
                        non_null_count=None,
                    )
                )
                continue
            non_null_count = profile_col.row_count - profile_col.null_count
            views.append(
                FreetextColumnView(
                    name=col_name,
                    strategy="passthrough",
                    dtype_known_non_string=not is_string_dtype_label(profile_col.dtype),
                    avg_length=profile_col.avg_length,
                    distinct_count=profile_col.distinct_count,
                    non_null_count=non_null_count,
                )
            )

    return tuple(
        score_unmasked_freetext(
            views, min_avg_length=min_avg_length, min_distinctness=min_distinctness
        )
    )


def _index_profile_columns(profile: Profile) -> dict[tuple[str, str], ColumnProfile]:
    """(table, column) -> ColumnProfile, for O(1) lookup per config column."""
    return {
        (table.name, column.name): column for table in profile.tables for column in table.columns
    }
