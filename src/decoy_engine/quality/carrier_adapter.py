"""The pandas boundary of the DP fit: `DataFrame -> CarrierTable` (DPS-CODEC
phase 2).

This is the ONLY module in the carrier layer that touches pandas/pyarrow, and
it is imported LAZILY (only when a DataFrame is passed to the fit) so the
direct-`CarrierTable` path in `carriers.py` stays pandas/pyarrow-free -- the
import-isolation invariant the whole redesign rests on (guide
docs/plans/2026-07-23-dps-codec-implementation-guide.md, sections 3.6/3.8).

WHY THIS MODULE EXISTS -- the bug class it kills. OpenDP proves
`(epsilon, delta)` over its input vector with STABILITY 1, and equivalence to
a per-ROW claim needs add/remove-one-row to change at most one released
element under OpenDP's atomic value equality. pandas derives a column's
storage dtype from ALL its rows, so one added row can rebox every existing
cell (int+None -> float64; bool -> object/complex when a `1j` is appended; an
Arrow `list` cell -> a length-1 ndarray when the column widens to object).
A value-level inference that read the BOX drifts on such an ordinary
neighbour while OpenDP's `map(1)` stays unchanged -- distance N on an N-row
column against a stability-1 certificate. The fix is to fetch each cell
DEFENSIVELY and route it through the single boxing-invariant codec per carrier
(`carriers.decode_number`/`decode_flag`/`decode_text`), whose verdict is a
function of the value modulo every reboxing. See `carriers.py` for the codec
invariants and Dwork & Roth, *Algorithmic Foundations of DP*, plus the OpenDP
transformation user guide, for the stability-1 methodology.

TOTALITY AND ITS RESIDUAL (section 3.7). The adapter is total over DATA
VALUES: no row's content makes it raise or warn, so fit success cannot become
a probability-0-vs-1 observable on a one-row neighbour. The per-cell FETCH is
itself content-dependent -- a `pd.ArrowDtype` timestamp/duration/date column
raises `OverflowError` on positional access when the stored integer leaves the
Python range -- so the fetch is guarded per position and a cell that cannot be
fetched is dropped (marked invalid), NOT allowed to abort the fit (carried
forward from `dp_normalize._cells`). The documented residual is unchanged:
`KeyboardInterrupt`/`SystemExit` raised from ANY invoked hook (the `__array__`
that null detection can invoke, or a cell's own dunders) propagate so an
operator can Ctrl-C and a caller can exit; a live container whose `array`/
`__len__` runs row-dependent caller code fails loud (out of domain); an
executable-object cell is not data. Warning suppression is process-global and
carries the single-threaded precondition.

The adapter's output is certified like any carrier: it is passed through
`carriers.sanitize_carrier_table()` before return, so a NaN/NUL/surrogate the
codecs would let through cannot reach OpenDP even if a future caller forgets
to sanitize. Sanitize re-runs the codecs, which is idempotent on the already-
canonical values the adapter produced.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from typing import Any

import numpy as np
import pandas as pd

# `_validate_bound` is the DP-canonical bound validator: reusing it (rather than
# re-deriving the finite/order rules here) guarantees the adapter's up-front,
# data-independent bound check agrees byte-for-byte with what `sanitize_carrier_
# table` enforces, so a malformed bound fails the SAME coded way regardless of
# which path validates it. Private cross-module use within the one carrier
# package, not a layer crossing.
from decoy_engine.quality.carriers import (
    CarrierError,
    CarrierTable,
    Column,
    FlagColumn,
    NumberColumn,
    TextColumn,
    _validate_bound,
    decode_flag,
    decode_number,
    decode_text,
    sanitize_carrier_table,
)

__all__ = ["dataframe_to_carrier_table"]

_VALID_CARRIERS = ("number", "flag", "text")

# The closed release-kind x carrier table (section 3.3). `kind` is OPTIONAL in
# `column_schema` -- the carrier alone drives the codec -- but when a caller
# supplies a `kind` it is validated against this table so an impossible pair
# (e.g. categorical + number, which OpenDP has no float `make_count_by` for)
# fails loud rather than silently mis-releasing.
_KIND_TO_CARRIERS: dict[str, tuple[str, ...]] = {
    "numeric": ("number",),
    "categorical": ("text", "flag"),
}


def _validate_column_schema(column_schema: Any) -> dict[str, tuple[float, float]]:
    """Validate `column_schema` structurally and return the coerced number
    bounds per column.

    Runs BEFORE any private cell is read, so a malformed schema/kind/bounds is
    a data-independent failure: an empty frame and its one-row neighbour fail
    identically, never reopening the fit-success channel. Number bounds are
    validated AND coerced to plain floats here so the decode loop never passes
    a non-float bound to the codec (which would raise from the clamp, mid-read,
    on a valid cell but not on an empty frame)."""
    if not isinstance(column_schema, dict):
        raise CarrierError(
            code="dp_schema_type",
            message=f"column_schema must be a dict, got {type(column_schema).__name__}",
        )
    number_bounds: dict[str, tuple[float, float]] = {}
    for name, spec in column_schema.items():
        if not isinstance(spec, dict):
            raise CarrierError(
                code="dp_schema_column_type",
                message=f"column {name!r}: schema entry must be a dict, got {type(spec).__name__}",
            )
        carrier = spec.get("carrier")
        if carrier not in _VALID_CARRIERS:
            raise CarrierError(
                code="dp_carrier_unknown",
                message=f"column {name!r}: unknown carrier {carrier!r}, expected one of {_VALID_CARRIERS}",
            )
        kind = spec.get("kind")
        if kind is not None:
            allowed = _KIND_TO_CARRIERS.get(kind)
            if allowed is None:
                raise CarrierError(
                    code="dp_kind_unknown",
                    message=f"column {name!r}: unknown kind {kind!r}, expected one of {tuple(_KIND_TO_CARRIERS)}",
                )
            if carrier not in allowed:
                raise CarrierError(
                    code="dp_kind_carrier_mismatch",
                    message=(
                        f"column {name!r}: kind {kind!r} does not allow carrier {carrier!r} "
                        f"(allowed: {allowed})"
                    ),
                )
        if carrier == "number":
            bounds = spec.get("bounds")
            if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
                raise CarrierError(
                    code="dp_carrier_bounds_missing",
                    message=f"column {name!r}: a 'number' carrier requires (lower, upper) bounds",
                )
            lower = _validate_bound(name, "lower", bounds[0])
            upper = _validate_bound(name, "upper", bounds[1])
            if not lower < upper:
                raise CarrierError(
                    code="dp_carrier_bounds_order",
                    message=f"column {name!r}: bounds must satisfy lower < upper, got ({lower}, {upper})",
                )
            number_bounds[name] = (lower, upper)
    return number_bounds


def _fetch_positional(series: pd.Series) -> Iterator[tuple[Any, bool]]:
    """Yield `(cell, fetched)` for each position, with the FETCH itself inside
    the guard (carried forward from `dp_normalize._cells`).

    The per-cell fetch is not free of content: a `pd.ArrowDtype`
    timestamp/duration/date/time column raises `OverflowError` on positional
    access whenever the stored integer leaves the Python range, and `[ns]` is
    the one safe resolution. `for raw in series` would put that fetch in the
    loop header where no `try` can reach it, so one out-of-range row would take
    the whole fit down -- a fit-success observable. Positional access with a
    per-cell guard drops just that cell (`fetched=False`), keeping the map
    recordwise; an `__iter__` that has raised cannot be resumed anyway.

    Setup (`series.array`, `len(series)`) is deliberately NOT guarded: it never
    raises for a real dtype, and a `Series` subclass whose `array`/`__len__`
    runs row-dependent caller code is out of domain (its container is as live
    as an executable cell) and must fail loud, not silently release a near-null
    column. The `yield` is deliberately OUTSIDE the guard so a finalization
    `GeneratorExit` propagates from the yield point and this generator stays
    conforming, while a fetch-raised `GeneratorExit` drops the cell like any
    other fetch failure."""
    values = series.array
    count = len(series)
    for i in range(count):
        cell: Any = None
        fetched = True
        try:
            cell = values[i]
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # broad by design: totality, see the module docstring
            fetched = False
        yield cell, fetched


def _is_null(cell: Any) -> bool:
    """Whether a fetched cell is a genuine scalar null (None/NaN/NaT/pd.NA).

    Deciding nullness runs the value's own hooks, so it can raise on content:
    `pd.isna(Decimal("sNaN"))` raises `InvalidOperation`, and `pd.isna` invokes
    a cell's `__array__`. A cell whose null check cannot be evaluated is treated
    as PRESENT and left to the codec, which is total. For an array-valued cell
    `pd.isna` returns an ARRAY of per-element verdicts rather than a verdict
    about the cell, so only a scalar result (`ndim == 0`) may exclude a row --
    `bool()` on a singleton container would otherwise silently read its one
    element. `KeyboardInterrupt`/`SystemExit` propagate (section 3.7)."""
    try:
        null = pd.isna(cell)
        return getattr(null, "ndim", 0) == 0 and bool(null)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # broad by design: totality, see the module docstring
        return False


def _build_number(series: pd.Series, row_count: int, lower: float, upper: float) -> NumberColumn:
    out_values = np.zeros(row_count, dtype=np.float64)
    out_validity = np.zeros(row_count, dtype=np.bool_)
    # Process-global warning suppression (section 3.7 single-threaded
    # precondition): no cell's content may leak a warning, which is itself an
    # observable. The codecs suppress internally too; this also covers the
    # fetch and `pd.isna`.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for i, (cell, fetched) in enumerate(_fetch_positional(series)):
            if not fetched or _is_null(cell):
                continue
            value, ok = decode_number(cell, lower=lower, upper=upper)
            out_values[i] = value
            out_validity[i] = ok
    return NumberColumn(values=out_values, validity=out_validity)


def _build_flag(series: pd.Series, row_count: int) -> FlagColumn:
    out_values = np.zeros(row_count, dtype=np.bool_)
    out_validity = np.zeros(row_count, dtype=np.bool_)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for i, (cell, fetched) in enumerate(_fetch_positional(series)):
            if not fetched or _is_null(cell):
                continue
            value, ok = decode_flag(cell)
            out_values[i] = value
            out_validity[i] = ok
    return FlagColumn(values=out_values, validity=out_validity)


def _build_text(series: pd.Series, row_count: int) -> TextColumn:
    out_values: list[str] = [""] * row_count
    out_validity = np.zeros(row_count, dtype=np.bool_)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for i, (cell, fetched) in enumerate(_fetch_positional(series)):
            if not fetched or _is_null(cell):
                continue
            value, ok = decode_text(cell)
            out_values[i] = value
            out_validity[i] = ok
    return TextColumn(values=tuple(out_values), validity=out_validity)


def dataframe_to_carrier_table(df: pd.DataFrame, column_schema: dict[str, dict]) -> CarrierTable:
    """Convert a pandas `DataFrame` to a certified `CarrierTable` (section 3.6).

    Fetches each cell of each schema column DEFENSIVELY, detects nulls, and
    routes every present cell through the matching boxing-invariant codec, so
    add/remove-one-row changes at most one element of each released vector --
    stability-1 by construction, subject to the section 3.7 residual. `df`'s
    length is the authoritative `row_count`, moved by exactly one on a
    neighbour. The result is passed through `sanitize_carrier_table` before
    return, so it is certified like any carrier.

    Structural problems (a non-DataFrame source, a malformed schema/kind/bounds,
    a schema column absent from the frame) fail loud with a coded `CarrierError`
    BEFORE any private cell is read, so they are data-independent."""
    if not isinstance(df, pd.DataFrame):
        raise CarrierError(
            code="dp_adapter_source_type",
            message=f"source must be a pandas DataFrame, got {type(df).__name__}",
        )
    number_bounds = _validate_column_schema(column_schema)
    missing = [name for name in column_schema if name not in df.columns]
    if missing:
        raise CarrierError(
            code="dp_adapter_missing_column",
            message=f"DataFrame is missing schema columns: {sorted(missing)}",
        )
    # A schema key that maps to two DataFrame columns makes `df[name]` a
    # DataFrame, not a Series; that is a structural (columns-level) problem, not
    # a row-value one, so reject it with a coded error before any cell read
    # rather than letting `_fetch_positional` raise a raw AttributeError.
    duplicated = [name for name in column_schema if list(df.columns).count(name) > 1]
    if duplicated:
        raise CarrierError(
            code="dp_adapter_duplicate_column",
            message=f"DataFrame has duplicate labels for schema columns: {sorted(duplicated)}",
        )
    row_count = len(df)
    columns: dict[str, Column] = {}
    for name, spec in column_schema.items():
        carrier = spec["carrier"]
        series = df[name]
        if carrier == "number":
            lower, upper = number_bounds[name]
            columns[name] = _build_number(series, row_count, lower, upper)
        elif carrier == "flag":
            columns[name] = _build_flag(series, row_count)
        else:  # "text" -- the only remaining member of _VALID_CARRIERS
            columns[name] = _build_text(series, row_count)
    raw = CarrierTable(row_count=row_count, columns=columns)
    return sanitize_carrier_table(raw, column_schema)
