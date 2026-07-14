"""Shared UNIQUE pool-capacity arithmetic (DE-11).

ONE definition of "is this pool big enough for a UNIQUE draw", imported by the
compile-time feasibility check (`_validate`), the S1 pre-flight
(`plan._checks`), and the runtime sampler (`_sampler`), so those layers can
never disagree on whether a UNIQUE draw fits.

UNIQUE places one distinct pool value in every NON-NULL output row, drawn
without replacement. The required capacity is therefore the number of non-null
output rows (``row_count - null_count``):

- NOT the source distinct count -- UNIQUE assigns a different value per emitted
  non-null row, so duplicate source values each still consume a pool value.
- NOT the total output row count -- preserved source nulls are re-emitted as
  null and consume no pool value.

Those three quantities were previously used interchangeably across compile and
runtime, so a column with duplicate source values or nulls could pass compile
and then fail (or be over-rejected) at runtime. DE-11 collapses them to this
one quantity.
"""

from __future__ import annotations


def unique_capacity_ok(pool_size: int, nonnull_output_rows: int) -> bool:
    """Whether a UNIQUE draw of ``nonnull_output_rows`` values fits in the pool.

    UNIQUE draws without replacement, so the pool must hold at least one
    distinct value per non-null output row. This is the single definition
    shared by the compile-time feasibility checks and the runtime sampler
    (DE-11); the source distinct count does not enter.
    """
    return pool_size >= nonnull_output_rows
