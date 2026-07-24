"""Typed carriers and total codecs for the DP fit (DPS-CODEC phase 1).

This is the pandas/pyarrow-free core of the differential-privacy redesign
(guide docs/plans/2026-07-23-dps-codec-implementation-guide.md, sections
3.1/3.2/3.7). It replaces the scattered pandas value-level type inference
of `dp_normalize.py` with one canonical typed-carrier layer converted
through a single total, boxing-invariant codec per carrier.

Established methodology (per CLAUDE.md's "use established methodology"
rule): OpenDP proves `(epsilon, delta)` over its input vector with
stability 1, and equivalence to a per-ROW claim needs add/remove-one-row
to change at most one vector element under OpenDP's ATOMIC value equality
(f64 equality, not byte-identity; String/bool equality for the categorical
domains) -- see the OpenDP transformation user guide and Dwork & Roth,
*Algorithmic Foundations of DP*. OpenDP assumes domain membership rather
than enforcing it, so a valid-marked NaN reaches `make_find_bin`, a NUL
text truncates, and a lone surrogate raises at the FFI. `sanitize_carrier_
table` re-applies the per-carrier FFI-safety checks so neither the pandas
adapter (phase 2) nor a directly-supplied `CarrierTable` can smuggle one
through. The boxing-invariance reasoning and its residual exclusions are
carried forward verbatim in intent from the retired `dp_normalize.py`
docstring and `docs/what-we-cannot-prove.md`.

TWO invariants, and the second is the one that keeps getting missed.

TOTALITY. Each codec returns a `(value, valid)` pair for ANY data cell and
never raises, so a row's content can never make the fit raise, warn, or
otherwise become an observable. Fit success is itself an observable: a
value that made one frame raise where its one-row neighbour succeeded would
break `(epsilon, delta)` for any delta < 1 before a single released number
is considered.

BOXING INVARIANCE. A codec's verdict is a function of the VALUE modulo
every reboxing pandas/numpy can apply. pandas derives a column's storage
from ALL its rows, so one added row can rebox every existing value
(int+None -> float; bool -> complex128 when a `1j` is appended; an Arrow
list cell -> a length-1 ndarray when the column widens to object). A codec
that read the box would drift on such an ordinary neighbour while OpenDP's
`map(1)` stayed unchanged. Canonicalize the scalar first, then decide.

THE DOMAIN, and the residual (section 3.7). The codecs catch
`BaseException` minus the two below and mark the cell invalid; that broad
catch is the total, content-independent path, not an oversight.
`KeyboardInterrupt` and `SystemExit` from ANY invoked hook (including the
`__array__` that null/shape detection can invoke, and `__float__`/`__str__`)
propagate, so an operator can Ctrl-C and a caller can exit. A cell that
raises either from its own dunders can terminate a fit where its one-row
neighbour succeeded and is therefore outside the adjacency domain. Live
container cells and non-canonical subclasses (this is why `TextColumn`
requires `type(v) is str`, not `isinstance`) are out of domain: a cell that
is executable code seizing process control is not data. Warning suppression
is process-global here and carries the documented single-threaded
precondition (a concurrent `simplefilter("error")` reopens the channel).
"""

from __future__ import annotations

import datetime
import math
import warnings
from dataclasses import dataclass
from typing import Any

import numpy as np

# Re-exported so the DP artifact schema is anchored in this pandas-free
# layer rather than in the pandas-bearing `snapshot.py` (guide section 3.8).
from decoy_engine.quality.dp_schema import DP_SNAPSHOT_SCHEMA_VERSION

__all__ = [
    "DP_SNAPSHOT_SCHEMA_VERSION",
    "CarrierError",
    "CarrierTable",
    "Column",
    "FlagColumn",
    "NumberColumn",
    "TextColumn",
    "decode_flag",
    "decode_number",
    "decode_text",
    "released_values",
    "sanitize_carrier_table",
]

_VALID_CARRIERS = ("number", "flag", "text")


class CarrierError(Exception):
    """A canonical-invariant violation the carrier layer cannot silently
    degrade past (guide section 3.1). Structural (a `bool` row_count, a
    schema/key mismatch, a dtype or length that is not what the carrier
    promises) rather than per-cell: those are out of the value domain and
    fail loud, whereas a per-cell FFI-safety failure only drops the cell.
    Carries a machine-readable code."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


# --- Boxing-invariant scalar predicates (numpy-only, no pandas) ------------


def _unbox(raw: Any) -> Any:
    """Strip the numpy scalar box, leaving the value it holds.

    pandas chooses the box from the whole column's contents, so the gate
    must see the value, not the box: `numpy.bool_` is neither `bool` nor a
    `numbers.Real`, so a naive type check drops an entire nullable-boolean
    column. `.item()` maps every numpy scalar to its Python equivalent.
    It is TYPE-ERASING, not gate-preserving, so callers must reject
    datetimelike values BEFORE calling it: `datetime64[ns].item()` returns
    an epoch INT, which would pass a real-number gate and release the
    timestamps themselves."""
    if isinstance(raw, np.generic):
        return raw.item()
    return raw


def _is_datetimelike(raw: Any) -> bool:
    """A time quantity has no categorical label and no numeric value.

    Checked BEFORE any unboxing, so the answer cannot depend on the
    resolution pandas chose. `pandas.Timestamp`/`Timedelta` subclass the
    `datetime` types; `numpy.datetime64`/`timedelta64` are caught by their
    dtype kind."""
    if isinstance(raw, (datetime.date, datetime.time, datetime.timedelta)):
        return True
    return getattr(getattr(raw, "dtype", None), "kind", None) in ("M", "m")


def _is_complex(raw: Any) -> bool:
    """A complex value is unconvertible whatever its width.

    `numpy.complex64` does not subclass Python `complex`, and `float()` on
    one silently returns the real part, so the dtype-kind check is needed
    to cover every numpy complex width."""
    if isinstance(raw, complex):
        return True
    return getattr(getattr(raw, "dtype", None), "kind", None) == "c"


def _is_container(raw: Any) -> bool:
    """A cell holding a SEQUENCE is not a scalar, whatever its length.

    `numpy.ndim` is the boxing-invariant test: 0 for every scalar the
    codecs accept and >= 1 for a `list`/`tuple`/multi-element `ndarray` at
    any length. A length-1 ndarray must drop here BEFORE `float()`, which
    would otherwise return its sole element -- the same logical value then
    converting in one boxing and dropping in another."""
    return np.ndim(raw) != 0


# --- The three codecs: total over one cell, invariant under every reboxing -

_INVALID_NUMBER: tuple[float, bool] = (0.0, False)
_INVALID_FLAG: tuple[bool, bool] = (False, False)
_INVALID_TEXT: tuple[str, bool] = ("", False)


def decode_number(value: Any, *, lower: float, upper: float) -> tuple[float, bool]:
    """Total number codec (guide section 3.2). Reject containers and
    temporals before unboxing; a zero-imaginary complex keeps its real
    part, a nonzero imaginary is invalid; NaN is invalid; +/-inf clamps to
    the bound; a finite out-of-domain value clamps into `[lower, upper]`;
    the result is a signed-zero-normalized binary64. Returns `(value,
    valid)`; the value at an invalid position is an arbitrary canonical
    `0.0`."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # section 3.7 single-threaded precondition
        try:
            if _is_container(value):
                return _INVALID_NUMBER
            if _is_datetimelike(value):
                # float(np.datetime64(...,'ns')) SUCCEEDS and returns the
                # epoch integer, so this needs its own rejection.
                return _INVALID_NUMBER
            if _is_complex(value):
                if value.imag != 0:
                    return _INVALID_NUMBER
                value = value.real
            f = float(value)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # broad by design: totality, see THE DOMAIN
            return _INVALID_NUMBER
    if math.isnan(f):
        return _INVALID_NUMBER
    if math.isinf(f):
        f = upper if f > 0 else lower
    else:
        f = min(max(f, lower), upper)
    if f == 0.0:
        f = 0.0  # normalize -0.0 -> 0.0 (OpenDP atomic f64 equality)
    return (f, True)


def decode_flag(value: Any) -> tuple[bool, bool]:
    """Total flag codec (guide section 3.2). Accepts `bool`, the exact
    int/float 0/1 reboxings, and a zero-imaginary complex whose real is
    exactly 0 or 1 (this is the bool->complex128 collapse a `1j` neighbour
    forces). Everything else -- strings/bytes (a `"1"` is not a flag),
    nonzero imaginary, 0.5, 2 -- is invalid."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            if _is_container(value):
                return _INVALID_FLAG
            if _is_datetimelike(value):
                return _INVALID_FLAG
            if isinstance(value, (str, bytes, bytearray)):
                # `float("1")` succeeds; text is never a flag value.
                return _INVALID_FLAG
            if _is_complex(value):
                if value.imag != 0:
                    return _INVALID_FLAG
                value = value.real
            f = float(value)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # broad by design: totality, see THE DOMAIN
            return _INVALID_FLAG
    if f == 0.0:
        return (False, True)
    if f == 1.0:
        return (True, True)
    return _INVALID_FLAG


def decode_text(value: Any) -> tuple[str, bool]:
    """Total text codec (guide section 3.2). Unicode only, verbatim (never
    `str()`): a numeric cell in a `text` column is invalid, not
    stringified. Requires `type(v) is str` exactly (a `str` subclass can
    override the very methods the FFI-safety check invokes). Rejects an
    embedded NUL (OpenDP truncates there) and a lone surrogate (not
    UTF-8-encodable, raises at the FFI)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            if _is_container(value):
                return _INVALID_TEXT
            v = value.item() if isinstance(value, np.generic) else value
            if type(v) is not str:
                return _INVALID_TEXT
            if "\x00" in v:
                return _INVALID_TEXT
            v.encode("utf-8")  # lone surrogate -> UnicodeEncodeError -> invalid
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # broad by design: totality, see THE DOMAIN
            return _INVALID_TEXT
    return (v, True)


# --- CarrierTable: physical shape and canonical invariants (section 3.1) ---


@dataclass(frozen=True)
class NumberColumn:
    """A numeric carrier: a float64 value ndarray and a bool validity mask."""

    values: np.ndarray
    validity: np.ndarray


@dataclass(frozen=True)
class FlagColumn:
    """A boolean carrier: a bool value ndarray and a bool validity mask."""

    values: np.ndarray
    validity: np.ndarray


@dataclass(frozen=True)
class TextColumn:
    """A text carrier: a tuple of `str` values and a bool validity mask."""

    values: tuple[str, ...]
    validity: np.ndarray


Column = NumberColumn | FlagColumn | TextColumn


@dataclass(frozen=True)
class CarrierTable:
    """A table of typed carriers plus the authoritative row count.

    `row_count` is the N released for the row-count query and is the
    projection add/remove-one-row must move by exactly one. A `CarrierTable`
    is only "valid" once `sanitize_carrier_table` has enforced the section
    3.1 invariants over it."""

    row_count: int
    columns: dict[str, Column]


def _check_validity(validity: Any, row_count: int, name: str) -> np.ndarray:
    if not isinstance(validity, np.ndarray) or validity.ndim != 1 or validity.dtype != np.bool_:
        raise CarrierError(
            code="dp_carrier_validity_shape",
            message=f"column {name!r}: validity must be a 1-D bool ndarray",
        )
    if validity.shape[0] != row_count:
        raise CarrierError(
            code="dp_carrier_validity_length",
            message=f"column {name!r}: validity length {validity.shape[0]} != row_count {row_count}",
        )
    return validity


def _sanitize_number(name: str, col: NumberColumn, spec: dict, row_count: int) -> NumberColumn:
    if not isinstance(col, NumberColumn):
        raise CarrierError(
            code="dp_carrier_type_mismatch",
            message=f"column {name!r}: declared carrier 'number' but column is {type(col).__name__}",
        )
    validity = _check_validity(col.validity, row_count, name)
    values = col.values
    if not isinstance(values, np.ndarray) or values.ndim != 1 or values.dtype != np.float64:
        raise CarrierError(
            code="dp_carrier_number_dtype",
            message=f"column {name!r}: NumberColumn values must be a 1-D float64 ndarray",
        )
    if values.shape[0] != row_count:
        raise CarrierError(
            code="dp_carrier_number_length",
            message=f"column {name!r}: values length {values.shape[0]} != row_count {row_count}",
        )
    try:
        lower, upper = spec["bounds"]
    except (KeyError, TypeError, ValueError) as exc:
        raise CarrierError(
            code="dp_carrier_bounds_missing",
            message=f"column {name!r}: a 'number' carrier requires (lower, upper) bounds",
        ) from exc
    out_values = np.zeros(row_count, dtype=np.float64)
    out_validity = np.zeros(row_count, dtype=np.bool_)
    for i in range(row_count):
        if not validity[i]:
            continue
        v, ok = decode_number(values[i], lower=lower, upper=upper)
        out_values[i] = v
        out_validity[i] = ok
    return NumberColumn(values=out_values, validity=out_validity)


def _sanitize_flag(name: str, col: FlagColumn, row_count: int) -> FlagColumn:
    if not isinstance(col, FlagColumn):
        raise CarrierError(
            code="dp_carrier_type_mismatch",
            message=f"column {name!r}: declared carrier 'flag' but column is {type(col).__name__}",
        )
    validity = _check_validity(col.validity, row_count, name)
    values = col.values
    if not isinstance(values, np.ndarray) or values.ndim != 1 or values.dtype != np.bool_:
        raise CarrierError(
            code="dp_carrier_flag_dtype",
            message=f"column {name!r}: FlagColumn values must be a 1-D bool ndarray",
        )
    if values.shape[0] != row_count:
        raise CarrierError(
            code="dp_carrier_flag_length",
            message=f"column {name!r}: values length {values.shape[0]} != row_count {row_count}",
        )
    out_values = np.zeros(row_count, dtype=np.bool_)
    out_validity = np.zeros(row_count, dtype=np.bool_)
    for i in range(row_count):
        if not validity[i]:
            continue
        v, ok = decode_flag(values[i])
        out_values[i] = v
        out_validity[i] = ok
    return FlagColumn(values=out_values, validity=out_validity)


def _sanitize_text(name: str, col: TextColumn, row_count: int) -> TextColumn:
    if not isinstance(col, TextColumn):
        raise CarrierError(
            code="dp_carrier_type_mismatch",
            message=f"column {name!r}: declared carrier 'text' but column is {type(col).__name__}",
        )
    validity = _check_validity(col.validity, row_count, name)
    values = col.values
    if not isinstance(values, tuple) or len(values) != row_count:
        raise CarrierError(
            code="dp_carrier_text_shape",
            message=f"column {name!r}: TextColumn values must be a length-{row_count} tuple",
        )
    out_values: list[str] = []
    out_validity = np.zeros(row_count, dtype=np.bool_)
    for i in range(row_count):
        if not validity[i]:
            out_values.append("")
            continue
        v, ok = decode_text(values[i])
        out_values.append(v)
        out_validity[i] = ok
    return TextColumn(values=tuple(out_values), validity=out_validity)


def sanitize_carrier_table(table: CarrierTable, column_schema: dict[str, dict]) -> CarrierTable:
    """Enforce the section 3.1 canonical invariants and re-apply the codec
    FFI-safety checks, returning a sanitized `CarrierTable`.

    Runs on EVERY input, including a directly-supplied carrier that bypasses
    the pandas adapter, so the direct path cannot smuggle a NaN/NUL/surrogate
    past OpenDP. Structural violations (a `bool` or negative `row_count`, a
    schema/key mismatch, a wrong column type, dtype or length) fail loud with
    a `CarrierError`: they are out of the value domain. A per-cell FFI-safety
    failure only DROPS that cell (validity -> False), which keeps sanitize
    recordwise and therefore adjacency-preserving."""
    rc = table.row_count
    if isinstance(rc, bool) or not isinstance(rc, int):
        raise CarrierError(
            code="dp_carrier_row_count_type",
            message=f"row_count must be a non-bool int, got {type(rc).__name__}",
        )
    if rc < 0:
        raise CarrierError(
            code="dp_carrier_row_count_negative",
            message=f"row_count must be non-negative, got {rc}",
        )
    if set(table.columns.keys()) != set(column_schema.keys()):
        raise CarrierError(
            code="dp_carrier_schema_mismatch",
            message=(f"column keys {sorted(table.columns)} != schema keys {sorted(column_schema)}"),
        )
    sanitized: dict[str, Column] = {}
    for name, spec in column_schema.items():
        carrier = spec.get("carrier")
        col = table.columns[name]
        if carrier == "number":
            sanitized[name] = _sanitize_number(name, col, spec, rc)
        elif carrier == "flag":
            sanitized[name] = _sanitize_flag(name, col, rc)
        elif carrier == "text":
            sanitized[name] = _sanitize_text(name, col, rc)
        else:
            raise CarrierError(
                code="dp_carrier_unknown",
                message=f"column {name!r}: unknown carrier {carrier!r}, expected one of {_VALID_CARRIERS}",
            )
    return CarrierTable(row_count=rc, columns=sanitized)


def released_values(table: CarrierTable) -> dict[str, list]:
    """The per-column vector that enters the OpenDP FFI: only the valid
    cells, in row order. Invalid positions contribute nothing, which is
    what makes an invalid neighbour row change no released element."""
    out: dict[str, list] = {}
    for name, col in table.columns.items():
        values = col.values
        validity = col.validity
        out[name] = [values[i] for i in range(table.row_count) if bool(validity[i])]
    return out
