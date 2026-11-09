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

from decoy_engine import reasons
from decoy_engine.execution._adapter import (
    column_draw_identity,
    context_draw_identity,
)
from decoy_engine.execution._positional_keys import positional_key_array
from decoy_engine.generation.pool._sampler import (
    _compiled_index_kernel,
    _reference_index_kernel,
    _validated_index_array,
)

_DOMAIN_CODE = reasons.runtime.FAKER_POSITION_OUT_OF_DOMAIN


def faker_selection_namespace(table: str, column: str, namespace: str | None) -> str:
    """The namespace that keys the draw: the configured one, else a per-column default.

    The default is `column_draw_identity` under a `faker-nd/` prefix. `None` and `""` both
    mean "not configured"."""
    if namespace:
        return namespace
    return f"faker-nd/{column_draw_identity(table, column)}"


def resolve_selection_namespace(ctx: Any, column: str, namespace: str | None) -> str:
    """`faker_selection_namespace` from the context, failing closed on an unstamped table."""
    if namespace:
        return namespace
    return f"faker-nd/{context_draw_identity(ctx, column, strategy='faker')}"


def positional_pool_indices(
    n: int,
    *,
    row_offset: int,
    job_seed: bytes,
    namespace: str,
    pool_size: int,
    gate_positions: np.ndarray[Any, Any] | None = None,
) -> np.ndarray[Any, Any]:
    """One pool index per full-table row, in one batch call.

    Without a `when:` gate the rows are `row_offset .. row_offset + n - 1`; under a gate they are
    the selected rows' full-table positions (C8-iii-d). Uses the compiled `derive_index_batch`
    when the native companion is present and the byte-identical reference otherwise, as
    `PoolSampler` does."""
    try:
        keys = positional_key_array(row_offset, n, code=_DOMAIN_CODE, gate_positions=gate_positions)
        kernel = _compiled_index_kernel() or _reference_index_kernel()
        idx: pa.Array = kernel.derive_index_batch(
            keys, mask_key=job_seed, namespace=namespace, pool_size=pool_size
        )
        return _validated_index_array(idx, expected_len=n, pool_size=pool_size)
    except BaseException:
        del job_seed
        raise


__all__ = [
    "faker_selection_namespace",
    "positional_pool_indices",
    "resolve_selection_namespace",
]
