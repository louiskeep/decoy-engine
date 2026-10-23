"""Fixed-width file writer: the inverse of `read_fixed_width`.

`write_fixed_width` turns a pandas DataFrame into a newline-delimited
fixed-width file per a `FixedWidthLayout`, such that
`read_fixed_width(path, layout)` reads back the exact frame it was given.
Fixed-width output is the one output direction the target schema used to
reject; this is the engine primitive callers (CLI / platform, in their own
slices) use to materialize a `FileTarget(format="fixed_width")`.

Inversion is made literal, not re-derived: a field is rendered and padded,
then the reader's OWN per-column decode (`_strip_pad` + `_cast_value`) is run
on the produced field and its result compared to the source value. The writer
accepts a value only when the reader reads it back unchanged. One source of
truth for the round-trip contract (the reader), so a change to how the reader
decodes automatically constrains what the writer will emit.

Fail-closed over a defined supported domain (the reader's: `str`/`int`/`float`
columns, single-char `pad`, `left`/`right` `align`, half-open `[start,
start + width)` char ranges, newline-delimited records). Any value that would
not round-trip exactly makes the whole write fail before anything is published:

- a rendered value wider than its column `width` (overflow);
- a string whose strip-side edge is the pad character, which the reader would
  strip away (e.g. align=left pad=" " value "ab "), or an integer whose
  trailing digits the pad-strip would remove (e.g. align=left pad="0" value 50);
- a null / NaN (the reader never emits one, so it cannot round-trip);
- a value whose Python type does not match the column's declared type;
- a string carrying an embedded newline (would split the record);
- a character that cannot encode as UTF-8 (the reader decodes UTF-8).

Publication is failure-safe: records are written to a temp file in the target
directory and `os.replace`d into place only after the whole frame has been
written and fsynced. A fail-closed failure (or any error) mid-frame removes the
temp file and leaves the canonical path untouched -- never a half-written file.

PII-safe errors, mirroring `_fixed_width_reader` exactly: the frame being
written may itself carry the PII this product masks. `_build_record` and
`_render_field` RETURN a failure message (row index, column name, rendered
value length -- never the value) instead of raising, and `write_fixed_width`
raises `FixedWidthWriteError` only after they have returned, from a frame that
holds no cell value. No raise happens inside an `except`/while a caster
exception is live, so the value-bearing `ValueError`/`UnicodeEncodeError` is
never attached as `__cause__`/`__context__`. See
docs/security/error-reporting-and-data-exposure.md.
"""

from __future__ import annotations

import numbers
import os
import tempfile
from math import isnan
from typing import Any

import pandas as pd

from decoy_engine.config._fixed_width import FixedWidthColumn, FixedWidthLayout
from decoy_engine.errors import FixedWidthWriteError
from decoy_engine.profile._fixed_width_reader import (
    _cast_value,
    _render_path,
    _resolve_layout,
    _strip_pad,
)

# The reader never reads the bytes between columns (every column is an absolute
# `[start, start + width)` slice), so any gap fill round-trips. A space is the
# conventional fixed-width filler and encodes to one UTF-8 byte.
_GAP_FILL = " "


def _render_value(value: Any, column: FixedWidthColumn) -> tuple[str, str | None]:
    """Render `value` to its unpadded field string for `column.type`, or return
    `(_, message)` when the value is not in the writable domain for that type.

    Never raises: the message is value-free (type name + length only) and the
    caller raises from a frame holding no value.
    """
    if column.type == "str":
        if not isinstance(value, str):
            return "", _type_message(column, value, want="str")
        if "\n" in value or "\r" in value:
            return "", (
                f"column {column.name!r}: str value (length {len(value)}) contains an "
                "embedded newline, which would split the fixed-width record"
            )
        return value, None
    if column.type == "int":
        # bool is a subclass of int; a True/False in an int column is a type
        # error, not the integer 1/0.
        if isinstance(value, bool) or not isinstance(value, numbers.Integral):
            return "", _type_message(column, value, want="int")
        return str(int(value)), None
    # float: a strictly-fractional real (float / numpy float / Decimal), never an
    # int (Integral) or bool. repr round-trips a Python float exactly.
    if not isinstance(value, numbers.Real) or isinstance(value, numbers.Integral):
        return "", _type_message(column, value, want="float")
    if isnan(value):
        # The reader fails a blank numeric rather than emitting NaN, so a NaN
        # cannot round-trip; refuse it explicitly rather than write "nan".
        return "", f"column {column.name!r}: float value is NaN, which has no round-trip encoding"
    return repr(float(value)), None


def _type_message(column: FixedWidthColumn, value: Any, *, want: str) -> str:
    """A value-free type-mismatch message (the Python type name is structural,
    not content; no value or length that could echo content is included)."""
    return (
        f"column {column.name!r}: expected a {want} value for a {column.type!r} column, "
        f"got {type(value).__name__}"
    )


def _pad_field(rendered: str, column: FixedWidthColumn) -> str:
    """Pad `rendered` to `column.width` with `column.pad` on the side implied by
    `column.align` (the inverse of the reader's `_strip_pad`)."""
    if column.align == "left":
        return rendered + column.pad * (column.width - len(rendered))
    return column.pad * (column.width - len(rendered)) + rendered


def _render_field(value: Any, column: FixedWidthColumn) -> tuple[str, str | None]:
    """Produce the padded, round-trip-verified field for `value`, or return
    `(_, message)` on any fail-closed condition. Never raises for data."""
    rendered, message = _render_value(value, column)
    if message is not None:
        return "", message
    if len(rendered) > column.width:
        return "", (
            f"column {column.name!r}: rendered value is {len(rendered)} chars, wider than "
            f"the column width {column.width} (overflow)"
        )
    try:
        rendered.encode("utf-8")
    except UnicodeEncodeError:
        # The exception text embeds the offending character; report only that a
        # char is unencodable, and never let it chain in (we are not in an
        # `except` that re-raises -- we return).
        return "", (
            f"column {column.name!r}: value (length {len(rendered)}) contains a character "
            "that cannot encode as UTF-8"
        )
    field = _pad_field(rendered, column)
    # Verify against the reader's own decode: the writer is correct iff the
    # reader reads back exactly what it wrote.
    stripped = _strip_pad(field, column)
    decoded, cast_failure = _cast_value(stripped, field, column, path="", line_no=0)
    if cast_failure is not None or not _equal(decoded, value):
        return "", (
            f"column {column.name!r}: value (length {len(rendered)}) does not round-trip "
            f"through the layout (pad={column.pad!r} align={column.align!r} width="
            f"{column.width}); the reader would decode it to a different value"
        )
    return field, None


def _equal(decoded: Any, original: Any) -> bool:
    """Round-trip equality for a decoded value vs the source value. NaN is
    rejected before this point, so a plain `==` is exact for the supported
    types; `bool(...)` collapses any numpy scalar truth value."""
    return bool(decoded == original)


def _build_record(
    row: dict[str, Any], spec: FixedWidthLayout, row_index: int
) -> tuple[str | None, str | None]:
    """Assemble one record line (no newline), or return `(None, message)` on the
    first fail-closed column. Never raises for data."""
    buffer = [_GAP_FILL] * spec.record_width
    for column in spec.columns:
        field, message = _render_field(row[column.name], column)
        if message is not None:
            return None, f"row {row_index}: {message}"
        buffer[column.start : column.start + column.width] = field
    return "".join(buffer), None


def write_fixed_width(
    frame: pd.DataFrame,
    layout: FixedWidthLayout | dict[str, Any],
    path: str | os.PathLike[str],
) -> str:
    """Write `frame` to a fixed-width file at `path` per `layout`, inverting
    `read_fixed_width`.

    Args:
        frame: a pandas DataFrame carrying (at least) every column the layout
            names; columns are written in layout order. A column present in the
            layout but missing from the frame, or a frame column the layout does
            not name, is a fail-closed error (never a silent drop). A zero-row
            frame writes an empty file.
        layout: a `FixedWidthLayout` or the equivalent validated-config dict
            (re-validated via `FixedWidthLayout.model_validate`, raising
            `ConfigError` on a malformed dict, exactly as the reader does).
        path: filesystem path to write. The file is written to a temp file in
            the same directory and `os.replace`d into place on full success, so
            a pre-existing file at `path` is replaced atomically and is left
            untouched if the write fails closed.

    Returns:
        The `path` written (as `str`), for caller convenience.

    Raises:
        TypeError: `frame` is not a pandas DataFrame, or `path` is not a str /
            `os.PathLike[str]`.
        ValueError: `path` contains a NUL character.
        ConfigError: `layout` is a dict that fails schema validation.
        FixedWidthWriteError: the frame's columns do not match the layout, or a
            value is outside the writable domain (overflow, pad-ambiguous,
            null/NaN, type mismatch, embedded newline, unencodable). Nothing is
            published in this case. The message is PII-safe (position + column +
            length, never the value).
        OSError: `path`'s directory does not exist or cannot be written, or the
            write fails part-way (passed through unwrapped; a mid-write OS
            failure is outside the data-error guarantee and its traceback frames
            may hold already-rendered records -- see the security doc).
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"frame must be a pandas DataFrame, got {type(frame).__name__}")
    path = os.fspath(path)
    if not isinstance(path, str):
        raise TypeError(f"path must be a str or os.PathLike[str], got {type(path).__name__}")
    if "\x00" in path:
        raise ValueError("path must not contain a NUL character")
    spec = _resolve_layout(layout, path=path)

    schema_message = _check_columns(frame, spec)
    if schema_message is not None:
        raise FixedWidthWriteError(schema_message)

    failure = _write_all(frame, spec, path)
    if failure is not None:
        # Raised here, after `_write_all` has returned and removed any temp file:
        # this frame holds only the message and path, never a cell value, and no
        # file was published.
        raise FixedWidthWriteError(failure)
    return path


def _check_columns(frame: pd.DataFrame, spec: FixedWidthLayout) -> str | None:
    """Return a value-free message if the frame's columns do not match the
    layout (missing or extra), else None. Column names are schema, not data."""
    frame_columns = list(frame.columns)
    layout_names = [column.name for column in spec.columns]
    missing = [name for name in layout_names if name not in frame_columns]
    if missing:
        return f"frame is missing layout column(s) {sorted(missing)}"
    extra = [str(name) for name in frame_columns if name not in layout_names]
    if extra:
        return (
            f"frame carries column(s) {sorted(extra)} the layout does not name; declare them "
            "in the layout or drop them before writing (no silent drop)"
        )
    return None


def _fsync_dir(directory: str) -> None:
    """Best-effort fsync of the directory so the os.replace rename survives a crash.
    A no-op where a directory fd cannot be opened or fsynced (e.g. Windows)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_all(frame: pd.DataFrame, spec: FixedWidthLayout, path: str) -> str | None:
    """Atomically write every record, or return a PII-safe failure message with
    nothing published. Raises only for genuine I/O errors (OSError)."""
    # `.tolist()` normalizes numpy scalars to Python types (int64 -> int, NaN ->
    # float('nan')), so the per-type `isinstance` checks in `_render_value` see
    # plain Python values.
    columns = {column.name: frame[column.name].tolist() for column in spec.columns}
    row_count = len(frame.index)
    rendered = _render_path(path)
    directory = os.path.dirname(os.path.abspath(path)) or "."

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".decoy-fw-", suffix=".tmp")
    try:
        failure: str | None = None
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            for row_index in range(row_count):
                row = {name: values[row_index] for name, values in columns.items()}
                line, message = _build_record(row, spec, row_index)
                if message is not None:
                    failure = f"{rendered}: {message}"
                    break
                if line is None:
                    # _build_record returns a line iff message is None, so this is
                    # unreachable; fail closed (publish nothing) rather than trust it.
                    failure = f"{rendered}: internal error (no record produced)"
                    break
                handle.write(line)
                handle.write("\n")
            if failure is None:
                handle.flush()
                os.fsync(handle.fileno())
        if failure is not None:
            # A data failure: drop the partial temp file and publish nothing.
            _silent_unlink(tmp_path)
            return failure
        os.replace(tmp_path, path)
        _fsync_dir(directory)
        return None
    except BaseException:
        _silent_unlink(tmp_path)
        raise


def _silent_unlink(tmp_path: str) -> None:
    """Remove a temp file, ignoring a missing-file error (best-effort cleanup;
    never masks the original exception being propagated)."""
    try:
        os.unlink(tmp_path)
    except OSError:
        pass


__all__ = ["write_fixed_width"]
