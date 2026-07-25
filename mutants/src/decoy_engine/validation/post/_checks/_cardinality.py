"""cardinality scan (engine-v2 S10): output distinct count vs the strategy contract.

Per the 8-scan table: `unique` mode -> all output values distinct (hard fail on a
repeat); `match_source_cardinality` -> output distinct should equal source distinct
(a deviation is a WARN, not a hard fail). Records a `DistinctCount` (source +
output) per masked column. Source distinct prefers the precomputed
`ColumnProfile.distinct_count`, falling back to scanning `sources` only where the
column was sampled (`distinct_count is None`) (Dennis ruling b).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.validation.post._scan import ScanContext, ScanOutcome, column_values
from decoy_engine.validation.post._types import DistinctCount

if TYPE_CHECKING:
    from decoy_engine.profile import ColumnProfile

_NAME = "cardinality"


def run_cardinality(ctx: ScanContext) -> ScanOutcome:
    distinct_counts: dict[str, DistinctCount] = {}
    warnings: list[QualityWarning] = []
    failed = False

    profile_lookup: dict[tuple[str, str], ColumnProfile] = {
        (table.name, col.name): col for table in ctx.profile.tables for col in table.columns
    }

    for table_name, table_seed in ctx.plan.seed_envelope.per_table:
        out_table = ctx.outputs.get(table_name)
        if out_table is None:
            continue
        for col_name, seed in table_seed.per_column:
            if col_name not in out_table.column_names:
                continue
            out_non_null = [v for v in column_values(out_table, col_name) if v is not None]
            output_distinct = len(set(out_non_null))
            source_distinct = _source_distinct(ctx, profile_lookup, table_name, col_name)
            key = f"{table_name}.{col_name}"
            distinct_counts[key] = DistinctCount(
                source_distinct=source_distinct, output_distinct=output_distinct
            )

            mode = seed.cardinality_mode
            if mode == "unique" and output_distinct != len(out_non_null):
                failed = True  # a UNIQUE column must have all-distinct output
            elif mode == "match_source_cardinality" and output_distinct != source_distinct:
                warnings.append(
                    QualityWarning(
                        code="cardinality_match_deviation",
                        # Scalar-transform columns carry no provider; the
                        # warning records the column, so "" is the empty marker.
                        provider=seed.provider or "",
                        column=col_name,
                        detail={
                            "table": table_name,
                            "source_distinct": source_distinct,
                            "output_distinct": output_distinct,
                        },
                    )
                )

    return ScanOutcome(
        name=_NAME, failed=failed, distinct_counts=distinct_counts, warnings=tuple(warnings)
    )


def _source_distinct(
    ctx: ScanContext,
    profile_lookup: Mapping[tuple[str, str], ColumnProfile],
    table_name: str,
    col_name: str,
) -> int:
    col_profile = profile_lookup.get((table_name, col_name))
    if col_profile is not None and col_profile.distinct_count is not None:
        return int(col_profile.distinct_count)
    src_table = ctx.sources.get(table_name)
    if src_table is None:
        return 0
    return len({v for v in column_values(src_table, col_name) if v is not None})
