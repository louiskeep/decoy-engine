"""Whole-column output reconciliation for the shadow coordinator's per-node loop.

Split out of `_shadow_coordinator.py` to keep that orchestration module under its
600-LOC cap (the same reason `_shadow_generation` / `_shadow_mixed` /
`_shadow_full_frame` were split out). `_assemble_column` is a pure function --
it reconciles a native operator's concatenated per-batch output onto the pandas
oracle's own schema-inference for this slice's degenerate shapes -- so it lives
here with the strategy-classification constants it reads.
"""

from __future__ import annotations

import pyarrow as pa

__all__ = ["assemble_column"]

# Tokenizing strategies build a fresh column: empty -> float64, all-null -> null,
# else -> string (passthrough is separate). bucket_perturb differs ONLY on empty
# (it passes its source object series through -> Arrow null), so it is split out.
# group_key never emits all-null (a null cell keys on "None") but its empty ->
# float64 rule matches, so it belongs here (the empty golden pins this).
_TOKENIZING_STRATEGIES = frozenset(
    {"redact", "truncate", "hash", "faker", "categorical", "group_key"}
)
_NULL_ON_EMPTY_STRATEGIES = frozenset({"bucket_perturb"})


def assemble_column(strategy: str, parts: list[pa.Array]) -> pa.Array:
    """Reconcile the concatenated native output onto the oracle's own
    pandas-round-trip schema for this slice's two degenerate shapes (C3): a
    zero-row column and an all-null (non-empty) column. `_batches` always
    returns at least one batch, so `parts` is never empty.

    Reads the type off `combined` itself (the REAL Arrow array the native
    operator produced), not a profile-resolved label: a profile built via a
    pandas read reports a null-bearing integer column as `float64` already
    (pandas' own int+NaN promotion happening one layer up, at profiling
    time), while the resident Arrow array the coordinator actually operates
    on stays `int64` with a validity bitmap -- Arrow has no trouble
    representing that. Using the array's own type is what makes this
    reconciliation track the oracle's real behavior instead of the
    profiler's.
    """
    combined = pa.concat_arrays(parts)
    n = len(combined)
    if strategy in _TOKENIZING_STRATEGIES:
        # redact / truncate / hash emit strings the native kernel produced: an
        # empty column round-trips through the pandas oracle as `float64`, an
        # all-null one as `null`, a normal one stays exactly as produced.
        if n == 0:
            return pa.array([], type=pa.float64())
        return pa.nulls(n, type=pa.null()) if combined.null_count == n else combined
    if strategy in _NULL_ON_EMPTY_STRATEGIES:  # empty + all-null -> null, else string
        return pa.nulls(n, type=pa.null()) if combined.null_count == n else combined
    # passthrough is value-identity, so its OUTPUT SCHEMA is exactly whatever
    # the pandas full-frame oracle infers when the table round-trips
    # `table.to_pandas()` -> `from_pandas(preserve_index=False)` (the oracle's
    # own mechanism, `_pandas_adapter.py`). Reproduce THAT -- a TABLE-level
    # round-trip, not an array-level `array.to_pandas()`: the two can diverge on
    # metadata-carrying dtypes across pandas versions (Codex final-gate:
    # nullable-int handling), while the single-column table round-trip matches
    # the oracle's per-column inference by construction for every admitted type
    # (large_string -> string, all-null bool/string -> null, int+null ->
    # float64, big-int/uint, empty -> pandas' own inference). The output equals
    # the ORACLE's output, not necessarily the source: passthrough itself never
    # masks, but the oracle's float64 promotion of a null-bearing integer loses
    # precision beyond 2**53 (e.g. 2**53+1 -> 2**53), and this reproduces that
    # exactly. So it reconciles the shadow to the oracle, never to the raw
    # source.
    normalized = pa.Table.from_pandas(pa.table({"c": combined}).to_pandas(), preserve_index=False)
    return normalized.column("c").combine_chunks()
