"""The CSV dialect and the one set of rules that decides whether a dialect is coherent.

`FileSource` (config validation) and `CsvDialect` (the public CSV reader) both call
`csv_dialect_problem`, so a rule is written and changed here only. No IO.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

from decoy_engine import reasons
from decoy_engine.errors import ConfigError


class _DialectDefaults(NamedTuple):
    has_header: bool
    column_names: None
    delimiter: str


# The one home of the dialect defaults. `CsvDialect`, the `FileSource` field defaults and the
# `FileSource` dump that omits defaults all read this, so a default changes in one place.
CSV_DIALECT_DEFAULTS = _DialectDefaults(has_header=True, column_names=None, delimiter=",")


NAMES_WITH_HEADER = (
    "`column_names` and `column_names_file` are forbidden when has_header=true "
    "(the header row already names the columns)"
)
HEADERLESS_WITHOUT_NAMES = (
    "has_header=false requires a non-empty `column_names` or a `column_names_file` "
    "(a headerless file has no names to read)"
)
BOTH_NAME_SOURCES = "set one of `column_names` or `column_names_file`, not both"

# `csv_dialect_problem` returns these texts as literals (the validation-site sentry pins literal
# returns by digest and cannot classify a constant), so the constants must stay equal to them;
# `test_dialect_problem_codes_match_returned_text` enforces it.
# Each coded branch text and its registered reason, side by side so neither moves alone.
# The name-list and delimiter checks stay uncoded.
DIALECT_PROBLEM_CODES = {
    NAMES_WITH_HEADER: reasons.config.SOURCE_FILE_COLUMN_NAMES_WITH_HEADER,
    HEADERLESS_WITHOUT_NAMES: reasons.config.SOURCE_FILE_NO_HEADER_COLUMNS,
    BOTH_NAME_SOURCES: reasons.config.SOURCE_FILE_COLUMN_NAMES_SOURCE_CONFLICT,
}


def csv_dialect_problem(
    has_header: bool | None,
    column_names: Sequence[str] | None,
    delimiter: str,
    *,
    column_names_file: str | None = None,
) -> str | None:
    """The first problem with this dialect as fixed text, or `None` when it is coherent.

    Text carries no prefix; each caller adds its own (`FileSource: ...`, `CsvDialect: ...`). The
    text is fixed and never echoes a submitted value, since the dialect is a public surface.

    `has_header` is `None` only in type: `FileSource` refuses a missing csv `has_header` in its
    field validator before this runs, and every other caller passes a real bool.
    """
    if column_names is not None and (
        isinstance(column_names, (str, bytes))
        or not isinstance(column_names, Sequence)
        or not all(isinstance(name, str) for name in column_names)
    ):
        # A bare string would split into characters; a generator is consumed once.
        return "`column_names` must be a list or tuple of strings"
    if column_names is not None and column_names_file is not None:
        return "set one of `column_names` or `column_names_file`, not both"
    if has_header and (column_names is not None or column_names_file is not None):
        return (
            "`column_names` and `column_names_file` are forbidden when has_header=true "
            "(the header row already names the columns)"
        )
    if not has_header and not column_names and column_names_file is None:
        # None or an empty list: an empty list would read a zero-column frame and
        # silently drop the whole file from profiling and masking.
        return (
            "has_header=false requires a non-empty `column_names` or a `column_names_file` "
            "(a headerless file has no names to read)"
        )
    if column_names is not None:
        # Names are the handle every mask rule keys on; a blank or duplicated name
        # would mislabel a column. Count-vs-file needs a read, so the reader guards that.
        if any(not name.strip() for name in column_names):
            return "`column_names` entries must be non-empty"
        if len(set(column_names)) != len(column_names):
            return "`column_names` must be unique"
    if len(delimiter) != 1:
        return "`delimiter` must be exactly one character"
    if delimiter in ("\n", "\r"):
        # A line terminator as the field delimiter has no coherent reader meaning.
        return "`delimiter` must not be a line terminator"
    if not delimiter.isprintable() and delimiter != "\t":
        # A NUL or other control character has no coherent single-char delimiter
        # meaning. Tab is the one control character that is a real delimiter (TSV).
        return "`delimiter` must be a printable character or a tab"
    return None


@dataclass(frozen=True)
class CsvDialect:
    """How a CSV source is laid out: header or declared names, and the field delimiter."""

    has_header: bool = CSV_DIALECT_DEFAULTS.has_header
    column_names: tuple[str, ...] | None = CSV_DIALECT_DEFAULTS.column_names
    delimiter: str = CSV_DIALECT_DEFAULTS.delimiter

    def __post_init__(self) -> None:
        # The rule runs on the raw value first, then the names are frozen to a tuple.
        problem = csv_dialect_problem(self.has_header, self.column_names, self.delimiter)
        if problem is not None:
            raise ConfigError(f"CsvDialect: {problem}")
        if self.column_names is not None:
            object.__setattr__(self, "column_names", tuple(self.column_names))


__all__ = [
    "BOTH_NAME_SOURCES",
    "CSV_DIALECT_DEFAULTS",
    "DIALECT_PROBLEM_CODES",
    "HEADERLESS_WITHOUT_NAMES",
    "NAMES_WITH_HEADER",
    "CsvDialect",
    "csv_dialect_problem",
]
