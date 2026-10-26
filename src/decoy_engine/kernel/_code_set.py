"""Named-Arrow code_set map: select each distinct non-null value once, expand with an Arrow take.

A code_set mask remaps every non-null cell to a corpus code. In the out-of-core route's admitted
shape (mask mode, no chapter_preserve) the remap is a PURE function of the value, so this helper
runs the selection once per DISTINCT non-null value and expands the result to one value per row
with `pyarrow.compute.take`, instead of one selection per row. For a batch of low-cardinality
codes (the clinical-code case) that is far fewer selections, and the output is byte-identical to
the per-cell loop: nulls stay null and in place, and a repeated input gets the same code it would
cell by cell (the selection depends only on the value and the corpus, not on row position).

This is a low-level kernel helper, like `kernel/_scalar.py`: it imports only Arrow and the shared
missing/pylist helpers, and takes the per-value selector as a callable, so it never depends on the
execution or transforms packages. The caller passes a closure over its resolved config and pinned
corpus record; this helper only deduplicates and expands.
"""

from __future__ import annotations

from collections.abc import Callable

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.kernel._scalar import _array_to_pylist, _is_missing


def code_set_map(
    values: pa.Array | pa.ChunkedArray, select: Callable[[str], str]
) -> pa.Array:
    """Map each non-null cell of `values` through `select`, calling it once per distinct value.

    `select(code) -> code` is the pure per-value corpus selection. The input is coerced to the
    oracle's own string representation first (Python `str()`, not an Arrow cast, which can spell a
    non-string differently), so a non-string source masks byte-identically to the per-cell loop.
    A missing cell (`_is_missing`) maps to null in place. Returns a `pa.string()` array of the same
    length as `values`."""
    keys = [
        None if _is_missing(value) else (value if isinstance(value, str) else str(value))
        for value in _array_to_pylist(values)
    ]
    # dictionary_encode puts the distinct non-null keys in the dictionary (null rows get a null
    # index), so `select` runs exactly once per distinct non-null value; take then scatters each
    # masked code back to its rows and leaves null rows null.
    encoded = pa.array(keys, type=pa.string()).dictionary_encode()
    masked = pa.array([select(code) for code in encoded.dictionary.to_pylist()], type=pa.string())
    return pc.take(masked, encoded.indices)
