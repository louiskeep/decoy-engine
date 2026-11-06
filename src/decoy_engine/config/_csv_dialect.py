"""The CSV dialect and the one set of rules that decides whether a dialect is coherent.

`FileSource` (config validation) and `CsvDialect` (the public CSV reader) both call
`csv_dialect_problem`, so a rule is written and changed here only. No IO.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from decoy_engine.errors import ConfigError


def csv_dialect_problem(
    has_header: bool, column_names: Sequence[str] | None, delimiter: str
) -> str | None:
    """The first problem with this dialect as fixed text, or `None` when it is coherent.

    Text carries no prefix; each caller adds its own (`FileSource: ...`, `CsvDialect: ...`). The
    text is fixed and never echoes a submitted value, since the dialect is a public surface.
    """
    if column_names is not None and (
        isinstance(column_names, (str, bytes))
        or not isinstance(column_names, Sequence)
        or not all(isinstance(name, str) for name in column_names)
    ):
        # A bare string would split into characters; a generator is consumed once.
        return "`column_names` must be a list or tuple of strings"
    if has_header and column_names is not None:
        return (
            "`column_names` is forbidden when has_header=true "
            "(the header row already names the columns)"
        )
    if not has_header and not column_names:
        # None or an empty list: an empty list would read a zero-column frame and
        # silently drop the whole file from profiling and masking.
        return (
            "has_header=false requires a non-empty `column_names` "
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

    has_header: bool = True
    column_names: tuple[str, ...] | None = None
    delimiter: str = ","

    def __post_init__(self) -> None:
        # The rule runs on the raw value first, then the names are frozen to a tuple.
        problem = csv_dialect_problem(self.has_header, self.column_names, self.delimiter)
        if problem is not None:
            raise ConfigError(f"CsvDialect: {problem}")
        if self.column_names is not None:
            object.__setattr__(self, "column_names", tuple(self.column_names))


__all__ = ["CsvDialect", "csv_dialect_problem"]
