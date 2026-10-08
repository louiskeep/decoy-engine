"""The dense uint64 key column for a position-keyed draw.

A position-keyed strategy draws for local row `i` the index kernel's draw for the canonical
integer `row_offset + i`. The offset domain is `[0, 2**64 - 1]`, which int64 cannot hold, so
the keys are a no-null `uint64` array. Categorical and Faker share this so the two cannot
disagree on the domain check or the dtype.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa

from decoy_engine.generation.pool import GenerationError

_UINT64_MAX = 2**64 - 1


def row_positions(
    row_offset: int, n: int, gate_positions: np.ndarray[Any, Any] | None, *, code: str
) -> np.ndarray[Any, Any]:
    """The full-table row number of each of this call's `n` rows, as a no-null `uint64` array.

    Without a `when:` gate the rows are contiguous: `row_offset .. row_offset + n - 1`. Under a
    gate the handler is given the selected subset, so the true row of local row `i` is
    `row_offset + gate_positions[i]` (the gate's frame-local positions, C8-iii-d). Keying on this
    instead of the match ordinal makes a selected row's draw independent of the predicate.
    """
    if gate_positions is not None:
        if len(gate_positions) != n:
            raise GenerationError(
                code=code,
                message=f"gate positions ({len(gate_positions)}) do not match row count ({n})",
            )
        base = np.asarray(gate_positions, dtype=np.uint64)
        hi = int(row_offset) + (int(base.max()) if n else 0)
        if row_offset < 0 or (n and hi > _UINT64_MAX):
            raise GenerationError(
                code=code,
                message=f"gated row {hi} leaves the uint64 position domain",
            )
        return np.uint64(row_offset) + base
    if row_offset < 0 or (n and row_offset + n - 1 > _UINT64_MAX):
        raise GenerationError(
            code=code,
            message=f"rows [{row_offset}, {row_offset + n}) leave the uint64 position domain",
        )
    return np.uint64(row_offset) + np.arange(n, dtype=np.uint64)


def positional_key_array(
    row_offset: int,
    n: int,
    *,
    code: str,
    gate_positions: np.ndarray[Any, Any] | None = None,
) -> pa.Array:
    """`uint64` key column for a positional draw, full-table-numbered (see `row_positions`)."""
    return pa.array(row_positions(row_offset, n, gate_positions, code=code), pa.uint64())


__all__ = ["positional_key_array", "row_positions"]
