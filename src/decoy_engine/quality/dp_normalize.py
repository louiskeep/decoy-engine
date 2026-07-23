"""Recordwise normalization for the DP fit (DPS Scope B).

Extracted from `quality/dp.py` to keep that module under the 600-LOC
orchestration cap, following the `dp_schedule.py`/`dp_ledger.py`
precedent. This is a cohesive unit rather than an arbitrary slice: it is
the whole boundary between caller-supplied row content and the values
OpenDP ever sees.

The invariant every function here maintains is TOTALITY. Each input row
contributes at most one element to the normalized vector, and no row's
content can make normalization raise, warn, or otherwise become an
observable. Fit success is itself an observable, so a value that made
one frame raise where its one-row neighbour succeeded would break
(epsilon, delta) for any delta < 1 before a single released number was
considered. Three separate review rounds found defects of exactly that
shape here, so prefer widening a guard over narrowing one.
"""

from __future__ import annotations

import math
import warnings
from typing import Any

import pandas as pd


def _is_complex(raw: Any) -> bool:
    """A complex value is unconvertible, whatever its width.

    `isinstance(raw, complex)` alone is not that test. `numpy.complex128`
    subclasses Python's `complex` and so is caught by it, but
    `numpy.complex64` does not, and `float()` on one silently returns the
    real part instead of failing. That is the "silently keep a real part"
    half of C-B2, still open for the narrower width. The dtype-kind check
    covers every numpy complex width, including any this build has not
    seen. Callers run this inside their conversion guard, so a value
    whose `dtype` access misbehaves drops the row rather than escaping.
    """
    if isinstance(raw, complex):
        return True
    return getattr(getattr(raw, "dtype", None), "kind", None) == "c"


def _normalize_numeric(series: pd.Series, *, lower: float, upper: float) -> list[float]:
    """Total, recordwise projection (guide section 4.2/7.1): every row
    contributes at most one element. Conversion failures and NaN become
    null (excluded); +-inf clamp to the declared bound; finite
    out-of-domain values clamp into [lower, upper]. Content can never
    raise -- there is no kind-selection branch here, only clamping.

    C-B2 (Codex round-3 blocker): this used to call the vectorized
    `pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)`. Codex
    demonstrated two content-dependent side channels in that path: (1) a
    column containing a Python/numpy `complex` value deterministically
    emits `ComplexWarning` on the cast to real, while an otherwise-
    identical neighbour without one emits no warning at all -- an
    observable with probability 0 on one neighbour and 1 on the other,
    which alone violates any (epsilon, delta) guarantee with delta < 1,
    independent of anything the fit releases; (2) `pd.to_numeric`'s
    object/complex coercion can silently produce garbage floats (an
    uninitialized buffer reinterpretation, not a `NaN`) instead of
    treating the value as a null conversion failure. Both close the same
    way: convert element-by-element with a blanket warning suppression
    around the whole pass, so NO warning is ever emitted regardless of
    content (not a per-type special case -- any future warning-emitting
    input closes the same way), and explicitly treat any `complex` value
    (covers the builtin type and `numpy.complex128`, which subclasses it)
    as an unconvertible failure before calling `float()`, so it becomes
    null like any other unconvertible value rather than silently keeping
    a real part.

    C-B4 (Codex round-4 blocker): the `except` here used to name
    `(TypeError, ValueError)`, which made the "content can never raise"
    claim above false. `float(10**10000)` raises `OverflowError`, so a
    one-row neighbour carrying a large Python int made the whole fit
    raise instead of emitting an artifact. Fit success/failure is itself
    an observable, and one with probability 0 on one neighbour and 1 on
    the other breaks (epsilon, delta) for any delta < 1 before a single
    released number is considered. Totality is the invariant, so the
    handler is now the totality itself rather than a list of the
    conversion errors we happened to think of: ANY failure converting a
    row's value drops that row, exactly as an unconvertible value
    already did. Each row still contributes at most one element, so
    add-or-remove-one-row stability is unchanged."""
    out: list[float] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for raw in series:
            try:
                if _is_complex(raw):
                    continue  # unconvertible, like any other non-numeric value
                v = float(raw)
            except Exception:  # broad by design: totality, see docstring
                continue
            if math.isnan(v):
                continue
            if math.isinf(v):
                v = upper if v > 0 else lower
            else:
                v = min(max(v, lower), upper)
            out.append(v)
    return out


def _normalize_categorical(series: pd.Series) -> list[str]:
    """Total, recordwise projection: nulls excluded via `Series.dropna()`
    (uniform across None/NaN/NaT/pd.NA), every remaining scalar mapped to
    its `str()` representation -- the one documented canonical string
    form for every supported scalar dtype declared categorical. Wrapped in
    the same blanket warning suppression as `_normalize_numeric` (C-B2):
    `str()` on an exotic scalar type is not known to warn today, but
    suppression here costs nothing and keeps both normalizers under the
    same "no warning ever, regardless of content" invariant.

    C-B4 (Codex round-4 blocker): `str()` was assumed total and called
    bare. It is not. CPython caps integer-to-string conversion at 4300
    digits, so `str(10**10000)` raises `ValueError`, and a one-row
    neighbour carrying such a value made the entire fit raise rather
    than emit an artifact -- the same probability-0-vs-1 fit-success
    channel closed in `_normalize_numeric`. Any failure rendering a
    row's value now drops that row, which is what an unrepresentable
    label should have meant all along; each row still contributes at
    most one element.

    Totality is defined against what the release boundary can consume,
    not merely against `str()` returning. A lone surrogate such as
    `"\\ud800"` is a perfectly good Python `str` that cannot be encoded
    as UTF-8, and it raised `UnicodeEncodeError` further downstream when
    the label crossed into OpenDP -- the same fit-success channel, just
    relocated. Labels are therefore required to be UTF-8 encodable here.
    `str.isascii()` is a cached flag check, so the overwhelmingly common
    all-ASCII label pays no encoding cost.

    Null exclusion is per row rather than a vectorized `Series.dropna()`
    for the same reason. Deciding nullness runs the value's own dunders,
    so it too can raise on content: `pd.isna(Decimal("sNaN"))` raises
    `InvalidOperation`, which took the whole fit down from outside the
    conversion guard. A row whose null check cannot be evaluated is
    treated as present and left to the conversion step, which is total.
    For an array-valued cell `pd.isna` returns an ARRAY of per-element
    verdicts rather than a verdict about the cell, so only a genuine
    scalar result (`ndim == 0`) is allowed to exclude a row; container
    cells stay present and are labelled by `str()`, matching what
    `dropna()` did. Testing `bool()` alone is not that check: it raises
    for a multi-element array (so those cells happened to stay present)
    but for a SINGLETON container it silently returns that one element's
    verdict, which dropped `[None]` and `numpy.array([numpy.nan])` where
    `dropna()` kept them (Codex round 5). Dropping them is still
    recordwise and does not weaken the DP claim, but the equivalence
    this function claims to `dropna()` was false."""
    out: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for raw in series:
            try:
                null = pd.isna(raw)
                if getattr(null, "ndim", 0) == 0 and bool(null):
                    continue
            except Exception:  # broad by design: totality, see docstring
                pass
            try:
                label = str(raw)
                if not label.isascii():
                    label.encode("utf-8")
            except Exception:  # broad by design: totality, see docstring
                continue
            out.append(label)
    return out
