"""Sentinel-preserve for the date operators (Phase D / D1c).

``provider_config["preserve_values"]`` lists exact ISO date literals (``YYYY-MM-DD``) that
``bucket_perturb`` and ``date_shift`` emit unchanged. A dataset marks rows with fixed sentinel
dates ("never expires" ``9999-12-31``, "unknown" ``2099-01-01``) and downstream logic tests
``date = <sentinel>``, so perturbing one breaks the check.

The carve-out is physical exclusion, not a post-hoc restore. Preserved rows are removed from the
batch before the operator parses, derives, shifts or formats anything, then their rendered
strings are scattered back. A restore after the fact is not enough: the oracle formats the whole
column at once, so a boundary sentinel still reaches pandas date arithmetic and crashes it.

Matching is date-granular and parses each batch once:

- pandas is the authority wherever it parses (the operators' own ``pd.to_datetime``), so a row is
  never matched differently from how it would otherwise be masked;
- a cell pandas turns into NaT gets one Python ``strptime`` attempt under the same format.
  That covers a date-only sentinel outside the pandas range (``9999-12-31`` is the one that
  matters) and nothing else, because the operators already pass a NaT cell through;
- a typed source (datetime64, ``datetime.date``) is compared by its calendar date.

The format domain is restricted for the same reason. ``%f`` and ``%Z`` cannot describe a
date-only sentinel and pandas and ``strptime`` disagree on them, and ``ISO8601`` / ``mixed`` are
pandas-only format names with no ``strftime`` meaning. A rendering format is also required, so a
preserved typed row has a defined string form: the output column stays one homogeneous string
column and never holds a raw date object.

Messages carry the column's shape and an entry index, never a literal or a cell value.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

PRESERVE_KEY = "preserve_values"

# YYYY-MM-DD only. Python 3.11 widened `date.fromisoformat` to basic and week forms, so the
# pattern keeps the accepted grammar identical on every supported interpreter.
_LITERAL = re.compile(r"\d{4}-\d{2}-\d{2}")

# `%f` and `%Z`: pandas and `strptime` disagree on them and neither describes a date-only
# sentinel. `ISO8601` and `mixed` are pandas-only `format=` values.
_UNSUPPORTED_DIRECTIVES = frozenset("fZ")
_PANDAS_ONLY_FORMATS = frozenset({"ISO8601", "mixed"})


def _directives(date_format: str) -> Iterator[str]:
    """The strftime directive letters of ``date_format``, with ``%%`` consumed as a literal."""
    i = 0
    while i < len(date_format) - 1:
        if date_format[i] == "%":
            if date_format[i + 1] != "%":
                yield date_format[i + 1]
            i += 2
            continue
        i += 1


def _is_set(raw: Any) -> bool:
    """Whether ``preserve_values`` is configured: absent, None and an empty list are all unset."""
    if raw is None:
        return False
    if isinstance(raw, (list, tuple)):
        return len(raw) > 0
    return True


def has_preserve_values(cfg: Mapping[str, Any]) -> bool:
    """Whether the config opts into ``preserve_values`` (absent, None and ``[]`` do not)."""
    return _is_set(cfg.get(PRESERVE_KEY))


def preserve_config_problem(cfg: Mapping[str, Any]) -> str | None:
    """Why the ``preserve_values`` config is invalid, or None when it is unset or valid.

    Checked in one place for the plan-compile checks, the handler preflights, the oracle and the
    native admission, so every route rejects the same configs with the same reason.
    """
    raw = cfg.get(PRESERVE_KEY)
    if not _is_set(raw):
        return None
    if not isinstance(raw, (list, tuple)):
        return f"{PRESERVE_KEY} must be a list of ISO dates (YYYY-MM-DD), got {type(raw).__name__}."
    for position, entry in enumerate(raw):
        if (
            not isinstance(entry, str)
            or _LITERAL.fullmatch(entry) is None
            or _parse_literal(entry) is None
        ):
            return (
                f"{PRESERVE_KEY} entry {position} is not a valid ISO date (YYYY-MM-DD). "
                "Write each sentinel as a real calendar date such as 9999-12-31."
            )
    date_format = cfg.get("date_format")
    if not isinstance(date_format, str) or date_format == "":
        return (
            f"{PRESERVE_KEY} requires an explicit date_format, so a preserved row has a "
            "defined text form. Add a date_format such as %Y-%m-%d."
        )
    if date_format in _PANDAS_ONLY_FORMATS:
        return (
            f"{PRESERVE_KEY} cannot be combined with date_format {date_format!r}, which has no "
            "strftime meaning. Use a concrete pattern such as %Y-%m-%d."
        )
    unsupported = sorted(set(_directives(date_format)) & _UNSUPPORTED_DIRECTIVES)
    if unsupported:
        return (
            f"{PRESERVE_KEY} cannot be combined with the date_format directive "
            f"'%{unsupported[0]}': it does not describe a date-only sentinel and pandas and "
            "Python parse it differently. Use a date-only pattern such as %Y-%m-%d."
        )
    return None


def _parse_literal(entry: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(entry)
    except ValueError:
        return None


def _is_typed_date(value: Any) -> bool:
    """A date-like object (not a string). ``pd.NaT`` subclasses ``datetime`` but is missing."""
    return isinstance(value, datetime.date) and value is not pd.NaT


def _calendar_date(value: datetime.date) -> datetime.date:
    return value.date() if isinstance(value, datetime.datetime) else value


def _parsed_hits(parsed: pd.Series, dates: frozenset[datetime.date]) -> np.ndarray[Any, Any]:
    """Which parsed cells fall on a preserved calendar date (NaT never does)."""
    if pd.api.types.is_datetime64_any_dtype(parsed.dtype):
        calendar: Any = parsed.dt.date
    else:  # mixed UTC offsets leave an object column of datetimes
        calendar = parsed.map(lambda v: None if v is pd.NaT or pd.isna(v) else v.date())
    return np.fromiter((d in dates for d in calendar), dtype=bool, count=len(parsed))


@dataclass(frozen=True)
class PreserveSet:
    """The resolved ``preserve_values`` of one column: literal dates plus the rendering format."""

    dates: frozenset[datetime.date]
    date_format: str

    def mask(self, series: pd.Series) -> np.ndarray[Any, Any]:
        """True where a row is a sentinel, in one matching pass over ``series``."""
        n = len(series)
        hits = np.zeros(n, dtype=bool)
        if n == 0:
            return hits
        if pd.api.types.is_datetime64_any_dtype(series.dtype):
            return _parsed_hits(series, self.dates)
        values = series.astype(object) if pd.api.types.is_extension_array_dtype(series) else series
        cells = values.to_numpy(dtype=object)
        is_text = np.fromiter((isinstance(v, str) for v in cells), dtype=bool, count=n)
        if is_text.any():
            hits[is_text] = self._text_hits(values[is_text])
        is_typed = np.fromiter((_is_typed_date(v) for v in cells), dtype=bool, count=n)
        for i in np.flatnonzero(is_typed):
            hits[i] = _calendar_date(cells[i]) in self.dates
        return hits

    def _text_hits(self, text: pd.Series) -> np.ndarray[Any, Any]:
        parsed = pd.to_datetime(text, format=self.date_format, errors="coerce")
        hits = _parsed_hits(parsed, self.dates)
        # pandas could not represent the cell. Retry only those under the same format with
        # Python's wider year range; each distinct text is parsed once.
        failed = parsed.isna().to_numpy()
        if failed.any():
            matching = {s for s in pd.unique(text[failed]) if self._strptime_date(s) in self.dates}
            if matching:
                hits |= failed & text.isin(matching).to_numpy()
        return hits

    def _strptime_date(self, text: str) -> datetime.date | None:
        try:
            return datetime.datetime.strptime(text, self.date_format).date()
        except (ValueError, OverflowError):
            return None

    def render(self, series: pd.Series, preserved: np.ndarray[Any, Any]) -> list[str]:
        """The output text of each preserved row.

        A string source is emitted byte for byte. A typed source is rendered from its calendar
        date with ``date_format`` so the output column stays homogeneous text.
        """
        out: list[str] = []
        for value in series.iloc[np.flatnonzero(preserved)]:
            if isinstance(value, str):
                out.append(value)
            else:
                out.append(self._format_date(_calendar_date(value)))
        return out

    def _format_date(self, value: datetime.date) -> str:
        return value.strftime(self.date_format)


def preserve_set_from_config(cfg: Mapping[str, Any]) -> PreserveSet | None:
    """The column's ``PreserveSet``, or None when ``preserve_values`` is unset or empty.

    Raises:
        ValueError: the config is invalid (see ``preserve_config_problem``).
    """
    problem = preserve_config_problem(cfg)
    if problem is not None:
        raise ValueError(problem)
    if not _is_set(cfg.get(PRESERVE_KEY)):
        return None
    dates = frozenset(d for d in (_parse_literal(e) for e in cfg[PRESERVE_KEY]) if d is not None)
    return PreserveSet(dates, str(cfg["date_format"]))


def apply_excluding_preserved(
    series: pd.Series,
    preserve: PreserveSet,
    compute: Callable[[pd.Series], pd.Series],
) -> tuple[pd.Series, int]:
    """Run ``compute`` on the non-preserved rows only and scatter the preserved text back.

    Returns ``(result, preserved_count)`` with ``result`` an object series on ``series``'s index.
    An empty remainder skips ``compute`` rather than asking it to handle zero rows.
    """
    preserved = preserve.mask(series)
    count = int(preserved.sum())
    if count == 0:
        return compute(series), 0
    result = series.astype(object).copy()
    rest = np.flatnonzero(~preserved)
    if rest.size:
        result.iloc[rest] = compute(series.iloc[rest]).to_numpy(dtype=object)
    result.iloc[np.flatnonzero(preserved)] = preserve.render(series, preserved)
    return result, count


__all__ = [
    "PRESERVE_KEY",
    "PreserveSet",
    "apply_excluding_preserved",
    "has_preserve_values",
    "preserve_config_problem",
    "preserve_set_from_config",
]
