"""Arrow-array scalar masking kernels for deterministic per-value strategies."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.determinism import derive
from decoy_engine.kernel._canonicalize import canonicalize_derive_source


def _is_missing(value: Any) -> bool:
    """True for a value the masking kernels must treat as missing (null).

    None and IEEE NaN both count, matching pandas ``isna()`` and the
    ``pa.array(..., from_pandas=True)`` conversion the full-frame pandas path
    runs BEFORE a value ever reaches these kernels (that conversion folds NaN
    to null). The out-of-core route feeds raw Arrow values straight in, so a
    float column carrying an actual NaN would otherwise be hashed / redacted /
    stringified ("nan") here where the oracle emitted null. Only NaN-like
    values are unequal to themselves, so ``value != value`` detects float,
    numpy-float, and Decimal('NaN') alike without special-casing each type; a
    non-comparable object simply is not missing.
    """
    if value is None:
        return True
    try:
        return bool(value != value)
    except Exception:
        return False


def _array_to_pylist(values: pa.Array | pa.ChunkedArray | list[Any]) -> list[Any]:
    """Normalize the kernel's input to a plain Python list of scalars.

    A real Arrow array/chunked-array is the common (fast) path. A caller may
    also pass a plain list of raw Python scalars: mixed-type pandas object
    columns (str and int values in one column) have no single Arrow type, so
    `pa.array(..., from_pandas=True)` can raise before ever reaching the
    kernel; the caller falls back to a raw Python list in that case (see
    `_hash.py`/`_truncate.py`) instead of duplicating the per-value logic
    below. Every kernel here is already per-value dispatch (canonicalize by
    Python type, or `str(value)`), so operating on raw scalars is no
    different from operating on `to_pylist()` output.
    """
    if isinstance(values, pa.ChunkedArray):
        return values.combine_chunks().to_pylist()
    if isinstance(values, pa.Array):
        return values.to_pylist()
    return list(values)


def passthrough_array(values: pa.Array | pa.ChunkedArray) -> pa.Array:
    """Return values as one Arrow array without changing logical values."""
    if isinstance(values, pa.ChunkedArray):
        return values.combine_chunks()
    return values


def hash_array(
    values: pa.Array | pa.ChunkedArray | list[Any],
    *,
    seed: bytes,
    namespace: str,
    truncate: int | None = None,
    derive_func=derive,
) -> pa.Array:
    """Mask non-null values with derive(seed, namespace, canonical(value)).hex()."""
    out: list[str | None] = []
    for value in _array_to_pylist(values):
        if _is_missing(value):
            out.append(None)
            continue
        token = derive_func(seed, namespace, canonicalize_derive_source(value)).hex()
        out.append(token[:truncate] if truncate is not None else token)
    return pa.array(out, type=pa.string())


def _redact_array_reference(
    values: pa.Array | pa.ChunkedArray,
    *,
    redact_with: Any = "REDACTED",
) -> pa.Array:
    """Replace every non-null value with ``redact_with``, one row at a time.

    Frozen: this is the pre-vectorization body of `redact_array`, kept verbatim
    as both the fallback for inputs the fast path declines and the oracle the
    parity tests check it against. Do not "improve" it; a behavior change here
    has to happen in lockstep with the fast path or the two silently diverge.
    """
    return pa.array(
        [None if _is_missing(value) else redact_with for value in _array_to_pylist(values)]
    )


def redact_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    redact_with: Any = "REDACTED",
) -> pa.Array:
    """Replace every non-null value with ``redact_with``.

    Takes a vectorized Arrow fast path only for the shape every shipped
    disguise actually uses -- a plain `pa.string()` array (or chunked array of
    one) and a ``str`` `redact_with` -- and only once that array passes a full
    UTF-8 validity check. Anything outside that closed guard (another dtype,
    an encoded-null layout such as dictionary/REE/union, invalid UTF-8, a
    non-Arrow list) falls back to `_redact_array_reference`, and any
    unforeseen Arrow error while computing the fast path does too, so the
    guard is total and non-raising: it either takes the fast path or produces
    exactly what the reference would have.
    """
    if type(redact_with) is str and isinstance(values, (pa.Array, pa.ChunkedArray)):
        try:
            v = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
            if v.type == pa.string():
                v.validate(full=True)
                missing = pc.is_null(v)  # type: ignore[attr-defined, unused-ignore]
                all_missing = pc.all(missing).as_py()  # type: ignore[attr-defined, unused-ignore]
                if len(v) == 0 or all_missing is True:
                    result: pa.Array = pa.nulls(len(v))
                else:
                    result = pc.if_else(  # type: ignore[attr-defined, unused-ignore]
                        missing, pa.scalar(None, pa.string()), pa.scalar(redact_with, pa.string())
                    )
                assert isinstance(result, pa.Array)  # noqa: S101 -- guard admits only pa.Array inputs
                return result
        except Exception:
            pass
    return _redact_array_reference(values, redact_with=redact_with)


def _truncate_array_reference(
    values: pa.Array | pa.ChunkedArray | list[Any],
    *,
    length: int,
    keep: str = "head",
    mask_char: str | None = None,
) -> pa.Array:
    """Apply the truncate strategy to non-null values, one row at a time.

    Frozen: this is the pre-vectorization body of `truncate_array`, kept
    verbatim as both the fallback for inputs the fast path declines and the
    oracle the parity tests check it against. Do not "improve" it; a behavior
    change here has to happen in lockstep with the fast path or the two
    silently diverge.
    """
    out: list[str | None] = []
    for value in _array_to_pylist(values):
        if _is_missing(value):
            out.append(None)
            continue
        text = str(value)
        if mask_char is None:
            out.append(text[-length:] if keep == "tail" else text[:length])
        elif keep == "tail":
            keep_part = text[-length:]
            drop_part = text[:-length] if length < len(text) else ""
            out.append((mask_char * len(drop_part)) + keep_part)
        else:
            keep_part = text[:length]
            drop_part = text[length:] if length < len(text) else ""
            out.append(keep_part + (mask_char * len(drop_part)))
    return pa.array(out, type=pa.string())


def truncate_array(
    values: pa.Array | pa.ChunkedArray | list[Any],
    *,
    length: int,
    keep: str = "head",
    mask_char: str | None = None,
) -> pa.Array:
    """Apply the truncate strategy to non-null values.

    Takes a vectorized Arrow fast path only for the shape every shipped
    disguise actually uses -- a plain `pa.string()` array (or chunked array of
    one), an in-range ``int`` `length`, ``keep`` in `("head", "tail")`, and a
    ``str`` (or ``None``) `mask_char` -- and only once that array passes a
    full UTF-8 validity check. Type checks are exact (`type(x) is ...`, not
    `isinstance`) so a `bool` length or a numpy int, which would otherwise
    pass an `isinstance(x, int)` check but mean something different, is
    excluded rather than silently mishandled; `keep` and `mask_char` are
    checked the same way so an unhashable `keep` (e.g. a list) cannot raise
    while testing membership. Anything outside that closed guard -- another
    dtype (including `large_string`), an out-of-range/non-int length, invalid
    UTF-8, a non-Arrow list -- falls back to `_truncate_array_reference`, and
    any unforeseen Arrow error while computing the fast path does too, so the
    guard is total and non-raising: it either takes the fast path or produces
    exactly what the reference would have.
    """
    if (
        type(length) is int
        and 0 < length <= 2**31 - 1
        and type(keep) is str
        and keep in ("head", "tail")
        and (mask_char is None or type(mask_char) is str)
        and isinstance(values, (pa.Array, pa.ChunkedArray))
    ):
        try:
            v = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
            if v.type == pa.string():
                v.validate(full=True)
                if mask_char is None:
                    if keep == "tail":
                        result = pc.utf8_slice_codeunits(  # type: ignore[attr-defined, unused-ignore]
                            v, -length, stop=None
                        )
                    else:
                        result = pc.utf8_slice_codeunits(  # type: ignore[attr-defined, unused-ignore]
                            v, 0, length
                        )
                else:
                    drop = pc.max_element_wise(  # type: ignore[attr-defined, unused-ignore]
                        pc.subtract(pc.utf8_length(v), length),  # type: ignore[attr-defined, unused-ignore]
                        0,
                    )
                    pad = pc.binary_repeat(  # type: ignore[attr-defined, unused-ignore]
                        pa.scalar(mask_char), pc.cast(drop, pa.int64())
                    )
                    if keep == "tail":
                        kept = pc.utf8_slice_codeunits(  # type: ignore[attr-defined, unused-ignore]
                            v, -length, stop=None
                        )
                        result = pc.binary_join_element_wise(  # type: ignore[attr-defined, unused-ignore]
                            pad, kept, pa.scalar(""), null_handling="emit_null"
                        )
                    else:
                        kept = pc.utf8_slice_codeunits(  # type: ignore[attr-defined, unused-ignore]
                            v, 0, length
                        )
                        result = pc.binary_join_element_wise(  # type: ignore[attr-defined, unused-ignore]
                            kept, pad, pa.scalar(""), null_handling="emit_null"
                        )
                if result.type != pa.string():
                    result = result.cast(pa.string())
                assert isinstance(result, pa.Array)  # noqa: S101 -- guard admits only pa.Array inputs
                return result
        except Exception:
            pass
    return _truncate_array_reference(values, length=length, keep=keep, mask_char=mask_char)
