"""Position-keyed selection for non-deterministic REUSE Faker.

The pool is built exactly as before. Only the SELECTION changes: for the non-null row at
ordinal `g = ctx.row_offset + i` the value is
`pool.values[derive_index(job_seed, selection_namespace, encode_int(g), pool.size)]`.
`encode_int` is the integer encoding `derive_index_batch` applies to a uint64 key column,
so a native route can later reproduce the draw byte for byte.

The key is `job_seed`, never `mask_key`: non-deterministic mode generates fresh synthetic
values and does not re-identify a source value, so it stays off the secret-derived key.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._positional_keys import positional_key_array
from decoy_engine.generation.pool._sampler import (
    _compiled_index_kernel,
    _reference_index_kernel,
    _validated_index_array,
)

_DOMAIN_CODE = "faker_position_out_of_domain"
_TABLE_UNKNOWN_CODE = "faker_positional_table_unknown"


def faker_selection_namespace(table: str, column: str, namespace: str | None) -> str:
    """The namespace that keys the draw: the configured one, else a per-column default.

    The default length-prefixes both parts. Names are unrestricted strings, so a plain
    `table/column` join would let table `a/b` + column `c` collide with table `a` + column
    `b/c`. `None` and `""` both mean "not configured"."""
    if namespace:
        return namespace
    return f"faker-nd/{len(table)}:{table}/{len(column)}:{column}"


def selection_column(ctx: Any, column: str) -> str:
    """The column identity for the default namespace: the outer column of a nested child."""
    return str(getattr(ctx, "nested_outer_column", "") or column)


def resolve_selection_namespace(ctx: Any, column: str, namespace: str | None) -> str:
    """`faker_selection_namespace` from the context, failing closed on an unstamped table."""
    if namespace:
        return namespace
    table = getattr(ctx, "current_table", "")
    if not table:
        raise StrategyError(
            code=_TABLE_UNKNOWN_CODE,
            strategy="faker",
            message=(
                f"column {column!r}: non-deterministic faker without a namespace keys its "
                "draw on the table and column, but the strategy context carries no current "
                "table. Every dispatch path stamps it; a caller that does not must set it."
            ),
        )
    return faker_selection_namespace(table, selection_column(ctx, column), namespace)


def positional_pool_indices(
    n: int, *, row_offset: int, job_seed: bytes, namespace: str, pool_size: int
) -> np.ndarray[Any, Any]:
    """One pool index per ordinal `row_offset .. row_offset + n - 1`, in one batch call.

    Uses the compiled `derive_index_batch` when the native companion is present and the
    byte-identical reference otherwise, as `PoolSampler` does."""
    keys = positional_key_array(row_offset, n, code=_DOMAIN_CODE)
    kernel = _compiled_index_kernel() or _reference_index_kernel()
    idx: pa.Array = kernel.derive_index_batch(
        keys, mask_key=job_seed, namespace=namespace, pool_size=pool_size
    )
    return _validated_index_array(idx, expected_len=n, pool_size=pool_size)


__all__ = [
    "faker_selection_namespace",
    "positional_pool_indices",
    "resolve_selection_namespace",
    "selection_column",
]
