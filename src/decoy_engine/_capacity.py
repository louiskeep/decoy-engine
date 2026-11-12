"""Shared UNIQUE pool-capacity arithmetic (DE-11).

ONE definition of "is this pool big enough for a UNIQUE draw", imported by the
compile-time feasibility check (`_validate`), the S1 pre-flight
(`plan._checks`), and the runtime sampler (`_sampler`), so those layers can
never disagree on whether a UNIQUE draw fits.

A foundation leaf: it imports nothing from the engine, so the config layer and
the generation layer both read it without one importing the other.
`generation.pool.unique_capacity_ok` is a facade over this function.

UNIQUE places one distinct pool value in every output row that must hold a
distinct value, drawn without replacement. For masking that is every NON-NULL
output row (``row_count - null_count``):

- NOT the source distinct count -- UNIQUE assigns a different value per emitted
  non-null row, so duplicate source values each still consume a pool value.
- NOT the total output row count -- preserved source nulls are re-emitted as
  null and consume no pool value.

Those three quantities were previously used interchangeably across compile and
runtime, so a column with duplicate source values or nulls could pass compile
and then fail (or be over-rejected) at runtime. DE-11 collapses them to this
one quantity.

Generate-side uniqueness (GEN-UNIQUE) calls the same rule with the rows that
must hold distinct values. A domain that contains a reusable SQL null can fill
any number of rows with that null, so ``null_reusable=True`` makes the capacity
unbounded. Callers pass it only when the null is inside the domain; a null
post-pass that blanks cells after the values are drawn is not capacity.

A per-parent maximum on a generated reference column is the same pigeonhole
question: ``max_per_parent * parents`` slots must hold every child row.
"""

from __future__ import annotations


def unique_capacity_ok(
    pool_size: int, nonnull_output_rows: int, null_reusable: bool = False
) -> bool:
    """Whether a UNIQUE draw of ``nonnull_output_rows`` values fits in the pool.

    UNIQUE draws without replacement, so the pool must hold at least one
    distinct value per row that must hold a distinct value. This is the single
    definition shared by the compile-time feasibility checks, the runtime
    sampler (DE-11) and the generate-side uniqueness checks; the source distinct
    count does not enter. ``null_reusable`` is True when the domain itself
    holds a SQL null that any number of rows may repeat, which makes the
    capacity unbounded.
    """
    if null_reusable:
        return True
    return pool_size >= nonnull_output_rows
