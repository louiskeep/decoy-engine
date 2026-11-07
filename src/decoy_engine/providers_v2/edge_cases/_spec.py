"""Edge-case catalogue entries and the admission rules every value must pass.

Pattern: Boundary-value analysis and curated naughty-string categories (ISO/IEC/IEEE 29119-4
two-value BVA; Big List of Naughty Strings categories, MIT, used as a reference only, no text
copied; Unicode UAX #15, UAX #9 and UTS #39 for the Unicode cases). See:
https://github.com/minimaxir/big-list-of-naughty-strings.

An ``EdgeCaseSpec`` checks every value when it is constructed, so a catalogue that breaks a rule
fails at import and no registry can be built from it. The rules:

- Text is valid Unicode (no surrogates, unassigned code points, noncharacters or private use).
  The only control characters are TAB, LF, and CR immediately followed by LF: pandas ``to_csv``
  quotes a field containing LF but not one containing only CR, so a lone CR splits the row.
- Format characters (Cf) come from an allowlist, and bidi embeddings, overrides and isolates
  are closed inside the value, so no value changes the direction of the text after it.
- No text value adds a read, write, delete or network operation of its own: after NFKC, no
  statement separator, pipe, chaining, comment, tautology, data-modifying keyword,
  resource-loading attribute, printf conversion, URL or lookup. A value spliced unquoted into a
  consumer's statement can still change or cut short that statement; no catalogue can prevent
  that, and those are the bugs these values exist to expose.
- Numbers are exact Python ``int`` within signed 64-bit, or finite ``float``. Dates are exactly
  ``datetime.date``. Datetimes are UTC-aware and inside the pandas nanosecond range, so a column
  of them is always ``datetime64[ns, UTC]`` (one value outside turns it into ``object`` and
  changes the Arrow type).

Errors name the provider, the category and the rule, never the value.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import struct
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

Kind = Literal["ascii_text", "unicode_text", "integer", "float", "date", "datetime"]

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
# First and last microseconds inside pandas' datetime64[ns] range.
DATETIME_MIN = dt.datetime(1677, 9, 21, 0, 12, 43, 145225, tzinfo=dt.timezone.utc)
DATETIME_MAX = dt.datetime(2262, 4, 11, 23, 47, 16, 854775, tzinfo=dt.timezone.utc)

_ALLOWED_CONTROLS = frozenset("\t\n\r")
_ALLOWED_FORMAT = frozenset(
    chr(c)
    for c in (
        0x00AD,
        *range(0x200B, 0x2010),
        *range(0x202A, 0x202F),
        0x2060,
        *range(0x2066, 0x206A),
        0xFEFF,
    )
)
_EMBED_OPENERS = frozenset("\u202a\u202b\u202d\u202e")
_EMBED_CLOSER = "\u202c"
_ISOLATE_OPENERS = frozenset("\u2066\u2067\u2068")
_ISOLATE_CLOSER = "\u2069"

# Matched after NFKC, case-insensitively; see the module docstring.
_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("disjunction", r"\bor\b"),
        ("tautology", r"'1'\s*=\s*'1'|\"1\"\s*=\s*\"1\"|\b1\s*=\s*1\b|\"\"\s*=\s*\"\""),
        ("separator", r"[;|&]"),
        ("comment", r"--|/\*|['\"]\s*#"),
        ("resource_attribute", r"\b(src|href|action|srcset|data|formaction)\s*="),
        ("url", r"://"),
        ("lookup", r"jndi"),
        (
            "modifying_keyword",
            r"\b(drop|delete|truncate|update|insert|alter|grant|exec|rm|curl|wget|nc)\b",
        ),
    )
)


@dataclass(frozen=True)
class EdgeCase:
    """One catalogue value and the category it exercises."""

    category: str
    value: Any


def _is_noncharacter(cp: int) -> bool:
    return 0xFDD0 <= cp <= 0xFDEF or (cp & 0xFFFE) == 0xFFFE


def _bidi_balanced(value: str) -> bool:
    stack: list[str] = []
    for ch in value:
        if ch in _EMBED_OPENERS:
            stack.append(_EMBED_CLOSER)
        elif ch in _ISOLATE_OPENERS:
            stack.append(_ISOLATE_CLOSER)
        elif ch in (_EMBED_CLOSER, _ISOLATE_CLOSER):
            if not stack or stack.pop() != ch:
                return False
    return not stack


def text_problem(value: object) -> str | None:
    """The first text rule ``value`` breaks, or None."""
    if type(value) is not str:
        return "not a str"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "not valid Unicode"
    for i, ch in enumerate(value):
        cat = unicodedata.category(ch)
        if cat == "Cc":
            if ch not in _ALLOWED_CONTROLS:
                return "control character"
            if ch == "\r" and value[i + 1 : i + 2] != "\n":
                return "lone CR"
        elif cat in ("Cs", "Co", "Cn") or _is_noncharacter(ord(ch)):
            return "surrogate, private-use, unassigned or noncharacter code point"
        elif cat == "Cf" and ch not in _ALLOWED_FORMAT:
            return "format character outside the allowlist"
    if not _bidi_balanced(value):
        return "unbalanced bidi control"
    folded = unicodedata.normalize("NFKC", value).replace("%%", "")
    if "%" in folded:
        return "printf conversion"
    for name, pattern in _FORBIDDEN:
        if pattern.search(folded):
            return f"forbidden pattern: {name}"
    return None


def _ascii_problem(value: object) -> str | None:
    problem = text_problem(value)
    if problem is None and not str(value).isascii():
        return "non-ASCII value in an ASCII provider"
    return problem


def _unicode_problem(value: object) -> str | None:
    problem = text_problem(value)
    if problem is None and str(value).isascii():
        return "ASCII-only value in the Unicode provider"
    return problem


def _integer_problem(value: object) -> str | None:
    if type(value) is not int:
        return "not an int"
    if not _INT64_MIN <= value <= _INT64_MAX:
        return "outside signed 64-bit"
    return None


def _float_problem(value: object) -> str | None:
    if type(value) is not float:
        return "not a float"
    if not math.isfinite(value):
        return "not finite"
    return None


def _date_problem(value: object) -> str | None:
    return None if type(value) is dt.date else "not exactly a datetime.date"


def _datetime_problem(value: object) -> str | None:
    if type(value) is not dt.datetime:
        return "not a datetime"
    if value.tzinfo is not dt.timezone.utc:
        return "not UTC-aware"
    if not DATETIME_MIN <= value <= DATETIME_MAX:
        return "outside the pandas nanosecond range"
    return None


_ADMIT: dict[Kind, Callable[[object], str | None]] = {
    "ascii_text": _ascii_problem,
    "unicode_text": _unicode_problem,
    "integer": _integer_problem,
    "float": _float_problem,
    "date": _date_problem,
    "datetime": _datetime_problem,
}


def _identity(kind: Kind, value: Any) -> object:
    # Bit pattern for floats: 0.0 == -0.0 under ==, but they are different edge cases.
    if kind == "float":
        return struct.pack("<d", value)
    return value


@dataclass(frozen=True)
class EdgeCaseSpec:
    """One provider: name, output kind, declared categories and the catalogue."""

    provider: str
    kind: Kind
    categories: tuple[str, ...]
    cases: tuple[EdgeCase, ...]

    def __post_init__(self) -> None:
        admit = _ADMIT[self.kind]
        seen: set[object] = set()
        for case in self.cases:
            if case.category not in self.categories:
                raise ValueError(f"{self.provider}: undeclared category {case.category!r}.")
            problem = admit(case.value)
            if problem is not None:
                raise ValueError(f"{self.provider}: a {case.category} value is refused: {problem}.")
            key = _identity(self.kind, case.value)
            if key in seen:
                raise ValueError(f"{self.provider}: a {case.category} value is a duplicate.")
            seen.add(key)
        used = {case.category for case in self.cases}
        for category in self.categories:
            if category not in used:
                raise ValueError(f"{self.provider}: category {category!r} has no values.")

    @property
    def values(self) -> tuple[Any, ...]:
        return tuple(case.value for case in self.cases)


def cases(category: str, *values: Any) -> tuple[EdgeCase, ...]:
    """Tag ``values`` with one category."""
    return tuple(EdgeCase(category, v) for v in values)
