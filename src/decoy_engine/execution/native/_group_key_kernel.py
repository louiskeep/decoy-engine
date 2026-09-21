"""Native deterministic group_key, wired to the canonicalize-free raw-hex kernel.

Phase 5 S-slate operator #2. The oracle (`transforms/group_key.py`) derives a
consistent key per row from a SIBLING `group_by` column's value:
`prefix + derive(mask_key, "group_key/<target>", str(group_by_value).encode())
[:length//2].hex()`. Rows sharing a group value get the same key. Two facts
drive this operator:

- The derivation is CANONICALIZATION-FREE: the oracle hashes the raw bytes of
  `str(value)`, unlike the canonicalizing `derive_batch`. So it routes through
  the dedicated compiled `derive_hex_raw_batch` kernel (`_group_key_ext.py`),
  never `derive_batch`.

- PANDAS is the stringify authority. The oracle does `str(raw_val)` per element
  over `df[group_by]`, where `df` is the pandas adapter's `to_pandas()` frame.
  `pc.cast` diverges from that (`"1"` vs pandas' `"1.0"`, `"true"` vs `"True"`,
  a preserved null vs `"None"`, tz `-0500` vs `-05:00`). A single formatter
  matches it byte-for-byte for every admitted sibling type: converting the
  column to a pandas Series and applying `Series.astype(str)` (the same path the
  oracle's frame takes). A null cell becomes the string `"None"`, so group_key
  never emits a null; its only degenerate shape is EMPTY.

v1 scope (see docs/plans/2026-09-21-native-group-key.md): the `group_by` sibling
must be an UNMASKED/passthrough column of a safe type (string, large_string,
int*, bool, date, timestamp; float/decimal/dictionary excluded), on the
FULL-FRAME route only; every other shape declines to the oracle at admission.
The runtime invariants below mirror `native_categorical` so a malformed compiled
kernel fails HERE, coded and fail-closed.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution._fk_keys import to_pandas_fk_safe
from decoy_engine.execution.native._group_key_ext import (
    RawHexDerivationKernel,
    load_compiled_raw_hex_kernel,
)
from decoy_engine.generation.pool import GenerationError

# A fixed, arbitrary single-column name for the stringify round-trip; the derived
# key never depends on it (astype(str) is per-value), only on the cell contents.
_STRINGIFY_COL = "gb"


def _stringify_sibling(col: pa.Array) -> pa.Array:
    """Stringify a group_by column exactly as the oracle's frame path does,
    giving a `pa.string()` array where a null cell becomes the oracle's own
    stringified null.

    The oracle does NOT use a plain `to_pandas()`: the pandas adapter routes a
    group_key `group_by` sibling through `to_pandas_fk_safe` (the lossless-typing
    contract, `_pandas_adapter.py`), which re-reads an INTEGER sibling as its own
    nullable pandas dtype (`int64` -> `Int64`, `uint64` -> `UInt64`, ...) rather
    than the float64-on-null default. That is byte-load-bearing here: a plain
    `to_pandas()` widens an int+null column to float64, so a null stringifies as
    `"nan"` and a value past 2**53 rounds, while the oracle sees `"<NA>"` and the
    exact integer. Applying the SAME conversion (then `astype(str)`, the proven
    per-type formatter) is what keeps the derived key byte-identical to the
    oracle for every admitted sibling type. Non-integer types are unchanged by
    `to_pandas_fk_safe`, matching the oracle's plain per-column inference for
    string/bool/date/timestamp.
    """
    df = to_pandas_fk_safe(pa.table({_STRINGIFY_COL: col}), {_STRINGIFY_COL})
    stringified = df[_STRINGIFY_COL].astype(str)
    return pa.array(stringified.to_numpy(), type=pa.string())


def native_group_key(
    group_by_array: pa.Array | pa.ChunkedArray,
    *,
    length: int,
    prefix: str,
    mask_key: bytes | None,
    namespace: str,
    native_threads: int | None = None,
    raw_hex_kernel: RawHexDerivationKernel | None = None,
) -> pa.Array:
    """Derive one consistent key per row from the `group_by` sibling column.

    `group_by_array` is the SIBLING column's data (the coordinator feeds
    `batch.column(group_by)`, not the target). Stringifies it with the oracle's
    `astype(str)` path, derives the raw (uncanonicalized) hex key via the
    compiled kernel, and prepends `prefix`. Output is always populated
    `pa.string()` (a null sibling cell yields the key for `"None"`, never a
    null); the empty-column reconciliation to the oracle's float64 inference
    happens at final assembly (`_shadow_coordinator._assemble_column`, the
    tokenizing branch).

    `namespace` is the SYNTHESIZED `f"group_key/{target}"` the caller resolved,
    NOT the plan namespace. `raw_hex_kernel` is loaded once here (mirroring
    `native_keyed_hash`'s own `load_compiled_crypto_kernel`) unless an override
    is injected for tests; a missing companion raises
    `CryptoExtensionUnavailableError`, which the caller maps to a coded decline.
    """
    if mask_key is None:  # pragma: no cover - require_mask_key never returns None
        raise AssertionError(
            "group_key reached with mask_key=None; require_mask_key always "
            "resolves a concrete key before the native route dispatches."
        )
    col = (
        group_by_array.combine_chunks()
        if isinstance(group_by_array, pa.ChunkedArray)
        else group_by_array
    )
    n = len(col)

    string_col = _stringify_sibling(col)
    kernel = raw_hex_kernel if raw_hex_kernel is not None else load_compiled_raw_hex_kernel()
    hex_out = kernel.derive_hex_raw_batch(
        string_col,
        mask_key=mask_key,
        namespace=namespace,
        hex_chars=length,
        native_threads=native_threads,
    )

    # Runtime invariants on the kernel's own result, mirroring `native_categorical`:
    # the isinstance/type check comes FIRST so a non-`pa.Array` result cannot leak
    # an uncoded AttributeError.
    if not isinstance(hex_out, pa.Array) or hex_out.type != pa.string():
        got = hex_out.type if isinstance(hex_out, pa.Array) else type(hex_out).__name__
        raise GenerationError(
            code="raw_hex_batch_type_mismatch",
            message=f"derive_hex_raw_batch returned {got}, expected a string Arrow array",
        )
    if len(hex_out) != n:
        raise GenerationError(
            code="raw_hex_batch_length_mismatch",
            message=f"derive_hex_raw_batch returned {len(hex_out)} keys for {n} input rows",
        )
    if hex_out.null_count != 0:
        # The stringify makes every cell non-null (a null becomes "None"), so the
        # kernel must return a fully-valid key column; a null here means the
        # never-null group_key invariant was violated upstream.
        raise GenerationError(
            code="raw_hex_batch_unexpected_null",
            message="derive_hex_raw_batch returned a null key for a populated string row",
        )

    if not prefix:
        return hex_out
    # Vectorized `prefix + key` per row; no nulls are present, so the default
    # emit-null handling never fires. The last positional argument is the
    # separator (empty), so each element is the plain concatenation.
    prefix_col = pa.array([prefix] * n, type=pa.string())
    # `binary_join_element_wise(a, b, sep)` concatenates a + sep + b per row; the
    # separator is the empty string, so each element is `prefix + key`.
    return pc.binary_join_element_wise(prefix_col, hex_out, "")  # type: ignore[attr-defined]


__all__ = ["native_group_key"]
