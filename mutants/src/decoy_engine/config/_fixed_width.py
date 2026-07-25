"""FixedWidthLayout: the declarative column-spec contract for fixed-width
`FileSource` inputs (S4, engine-finish-open-ended program).

This schema is a CONTRACT: the platform's Files/Layouts persistence
(S7) stores and round-trips exactly this shape (as JSON, or as YAML a
customer authors by hand and the platform loads into the same shape).
Keep it minimal -- every field earns its place.

Design decisions (pin these; do not silently redefine them elsewhere):

- `start` is 0-based. Together with `width` it defines a half-open
  byte range `[start, start + width)` -- the same convention as Python
  string slicing (`line[start:start + width]`) and pandas.read_fwf's
  `colspecs`, so there is exactly one way to read the numbers.
- Record model: newline-delimited. Each line in the file is one
  record; every column is a fixed slice of that line. No multi-line
  records, no COBOL copybook import (that is an explicit, separate
  follow-on -- see the program plan).
- Supported `type`s: `str` (default), `int`, `float`. This is the
  minimal set a hand-authored layout needs; a wider set is a deliberate
  future addition, not an oversight.
- Columns must be listed in ascending, non-overlapping `start` order.
  A column whose `start` falls before the previous column's end is
  rejected as malformed -- this single rule catches both overlapping
  ranges and out-of-order columns in one check. Gaps between columns
  (unused byte ranges) are allowed.

Known gaps -- documented, not fixed here (S4 scope; flagged for S7):

- **Trailing bytes past the last column's end are tolerated by
  design.** The reader only rejects a record *shorter* than
  `record_width` (row-width mismatch); a record *longer* than that has
  its extra trailing bytes silently ignored, matching the plain
  `(start, width)`-slicing convention this layout uses (there is no
  "record length" field to violate). This is NOT the same thing as
  detecting a **misaligned** layout: a layout whose columns are too
  narrow, or offset by a constant amount, for the actual data will
  still slice and cast "successfully" -- it will simply read the wrong
  bytes into each column, with no error. There is no structural way to
  detect this from the spec alone (a too-narrow layout looks identical
  to a correct one that happens to have a shorter `record_width`). The
  S7 authoring UI is the intended mitigation: render a parse-preview
  (a handful of sample records rendered through the candidate layout)
  so a human can visually catch misalignment before saving it, rather
  than the engine trying to infer it.
- **An all-pad/whitespace numeric field is an honest parse error, not
  a zero.** A genuine zero-padded numeric (e.g. pad="0", "0000")
  parses to `0`; a numeric field that is blank in the source data
  (all spaces, or all of whatever `pad` character is declared, with no
  digits) still raises `FixedWidthParseError` rather than being
  silently coerced to `0` -- see
  `profile._fixed_width_reader._cast_value` for the exact rule.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FixedWidthColumn(BaseModel):
    """One column of a fixed-width record layout.

    `start` + `width` describe the half-open byte range `[start, start
    + width)` within each record. `pad` is the single fill character
    used to pad the value out to `width` (default space); `align`
    tells the reader which side the pad lives on and therefore which
    side to strip: `"left"` (default) means the value is left-justified
    with padding on the right, so the reader `rstrip`s; `"right"` means
    the value is right-justified with padding on the left, so the
    reader `lstrip`s.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    start: int = Field(..., ge=0)
    width: int = Field(..., gt=0)
    type: Literal["str", "int", "float"] = "str"
    pad: str = Field(default=" ", min_length=1, max_length=1)
    align: Literal["left", "right"] = "left"


class FixedWidthLayout(BaseModel):
    """An ordered column-spec for a fixed-width file.

    See the module docstring for the frozen design decisions (0-based
    `start`, newline-delimited record model, the `str | int | float`
    type set). `columns` must be non-empty, every name unique, and
    every column's `start` at or after the previous column's end
    (`start + width`) -- overlapping or out-of-order columns fail loud
    at construction time, before any file is ever read.
    """

    model_config = ConfigDict(extra="forbid")

    columns: list[FixedWidthColumn] = Field(..., min_length=1)

    @property
    def record_width(self) -> int:
        """The minimum line length this layout requires (the last
        column's `start + width`)."""
        return max(column.start + column.width for column in self.columns)

    @model_validator(mode="after")
    def _validate_column_order(self) -> FixedWidthLayout:
        seen_names: set[str] = set()
        prev_end = 0
        for column in self.columns:
            if column.name in seen_names:
                raise ValueError(f"fixed_width layout: duplicate column name {column.name!r}")
            seen_names.add(column.name)
            if column.start < prev_end:
                raise ValueError(
                    f"fixed_width layout: column {column.name!r} (start={column.start}) "
                    f"overlaps a preceding column or is out of order (preceding column "
                    f"ends at {prev_end}); columns must be listed in ascending, "
                    "non-overlapping start order"
                )
            prev_end = column.start + column.width
        return self
