"""The dense uint64 key column for a position-keyed draw.

A position-keyed strategy draws for local row `i` the index kernel's draw for the canonical
integer `row_offset + i`. The offset domain is `[0, 2**64 - 1]`, which int64 cannot hold, so
the keys are a no-null `uint64` array. Categorical and Faker share this so the two cannot
disagree on the domain check or the dtype.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa

from decoy_engine.generation.pool import GenerationError

_UINT64_MAX = 2**64 - 1


def positional_key_array(row_offset: int, n: int, *, code: str) -> pa.Array:
    """`uint64` keys `row_offset .. row_offset + n - 1`; `code` names the domain error."""
    if row_offset < 0 or (n and row_offset + n - 1 > _UINT64_MAX):
        raise GenerationError(
            code=code,
            message=f"rows [{row_offset}, {row_offset + n}) leave the uint64 position domain",
        )
    return pa.array(np.uint64(row_offset) + np.arange(n, dtype=np.uint64), pa.uint64())


__all__ = ["positional_key_array"]
