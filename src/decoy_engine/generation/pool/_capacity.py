"""Compatibility facade for the UNIQUE pool-capacity rule.

The rule is `decoy_engine._capacity.unique_capacity_ok`, a foundation leaf the config layer
can read too. This module keeps the import path the pool package and its callers already use.
"""

from __future__ import annotations

from decoy_engine import _capacity


def unique_capacity_ok(
    pool_size: int, nonnull_output_rows: int, null_reusable: bool = False
) -> bool:
    """Whether a draw of ``nonnull_output_rows`` distinct values fits in the pool.

    Delegates to the foundation rule at call time, so the pool and the config checks cannot
    disagree. ``null_reusable`` is True when the domain itself holds a reusable SQL null.
    """
    return _capacity.unique_capacity_ok(pool_size, nonnull_output_rows, null_reusable)
