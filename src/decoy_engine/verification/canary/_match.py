"""Span matcher of ``canary-span-normalization/v1``: rules M1, M2 and M4 over M3 text.

- M1. Exact patterns are the canaries as given. Span patterns are those whose raw and M3 forms
  both have at least ``span_min_scalars`` code points; the rest are short-excluded and still
  match ``exact_cell``.
- M2. ``exact_cell`` is a ``frozenset`` lookup. The span modes use the Karp and Rabin (1987)
  fixed-length window filter: a ``dict`` from the first ``span_min_scalars`` code points of
  each pattern to the patterns with that prefix is the hash table, every window of the cell is
  looked up, and ``str.startswith`` is the verify step. The first hit ends the cell. Chosen over
  a pure-Python Aho-Corasick because the slicing and lookup run in C.
- M4. Modes are tried strongest first and a cell counts once, under the first that matches
  (invariant I5). A cell over ``MAX_CELL_UTF8_BYTES`` of UTF-8 is oversized and not compared.
  A cell holding a post-13.0 table code point gets the exact modes only; if neither matches it
  is uncompared, never silently clear.

Patterns are built per call and live only as long as the caller holds them: no module-level
cache (defect class 3). Outcomes and counts carry no canary or cell text, and the pattern
holder's ``repr`` shows counts only (invariant I1).
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass, field

from decoy_engine.verification.canary._normalize import holds_post_13_0, normalize_m3
from decoy_engine.verification.canary._types import CanaryMatchMode, CanaryPolicyInputError

MAX_CELL_UTF8_BYTES = 1024 * 1024
_MAX_UTF8_BYTES_PER_SCALAR = 4


class CellOutcome(enum.Enum):
    """One outcome per cell; the first three are findings, in match-mode order."""

    EXACT_CELL = "exact_cell"
    EXACT_SPAN = "exact_span"
    NFC_WHITESPACE_SPAN = "nfc_whitespace_span"
    NO_MATCH = "no_match"
    OVERSIZED = "oversized"
    UNCOMPARED = "uncompared"

    @property
    def match_mode(self) -> CanaryMatchMode | None:
        return _MODE_OF.get(self)


_MODE_OF = {
    CellOutcome.EXACT_CELL: CanaryMatchMode.EXACT_CELL,
    CellOutcome.EXACT_SPAN: CanaryMatchMode.EXACT_SPAN,
    CellOutcome.NFC_WHITESPACE_SPAN: CanaryMatchMode.NFC_WHITESPACE_SPAN,
}


@dataclass(frozen=True, slots=True)
class PatternCounts:
    exact_pattern_count: int
    normalized_pattern_count: int
    normalization_collision_count: int
    excluded_short_canary_count: int


@dataclass(frozen=True, slots=True)
class CanaryPatterns:
    """Call-scoped pattern sets. The text fields are kept out of ``repr`` and ``str``."""

    counts: PatternCounts
    window: int
    exact: frozenset[str] = field(repr=False)
    span_index: dict[str, tuple[str, ...]] = field(repr=False)
    normalized_index: dict[str, tuple[str, ...]] = field(repr=False)


@dataclass(frozen=True, slots=True)
class CellTally:
    exact_cell: int
    exact_span: int
    nfc_whitespace_span: int
    no_match: int
    oversized: int
    uncompared: int

    @property
    def finding_cell_count(self) -> int:
        return self.exact_cell + self.exact_span + self.nfc_whitespace_span

    def as_counts(self) -> dict[str, int]:
        return {o.value: getattr(self, o.value) for o in CellOutcome}


def screen_canaries(canaries: Iterable[object]) -> bool:
    """False if any canary is not a ``str`` or holds a post-13.0 table code point."""
    return all(type(c) is str and not holds_post_13_0(c) for c in canaries)


def build_patterns(canaries: tuple[str, ...], span_min_scalars: int) -> CanaryPatterns:
    """Pattern sets for one scan call. Inputs must already be screened (M3 table, types)."""
    if type(canaries) is not tuple or type(span_min_scalars) is not int or span_min_scalars < 1:
        raise CanaryPolicyInputError
    if not screen_canaries(canaries):
        raise CanaryPolicyInputError
    window = span_min_scalars
    exact = frozenset(canaries)
    span: dict[str, list[str]] = {}
    normalized: dict[str, list[str]] = {}
    span_patterns = 0
    short = 0
    for canary in dict.fromkeys(canaries):
        norm = normalize_m3(canary)
        if norm is None:
            raise CanaryPolicyInputError
        if len(canary) < window or len(norm) < window:
            short += 1
            continue
        span_patterns += 1
        span.setdefault(canary[:window], []).append(canary)
        group = normalized.setdefault(norm[:window], [])
        if norm not in group:
            group.append(norm)
    distinct_normalized = sum(len(g) for g in normalized.values())
    counts = PatternCounts(
        exact_pattern_count=len(exact),
        normalized_pattern_count=distinct_normalized,
        normalization_collision_count=span_patterns - distinct_normalized,
        excluded_short_canary_count=short,
    )
    return CanaryPatterns(
        counts=counts,
        window=window,
        exact=exact,
        span_index={k: tuple(v) for k, v in span.items()},
        normalized_index={k: tuple(v) for k, v in normalized.items()},
    )


def _oversized(cell: str) -> bool:
    n = len(cell)
    if n * _MAX_UTF8_BYTES_PER_SCALAR <= MAX_CELL_UTF8_BYTES:
        return False
    if n > MAX_CELL_UTF8_BYTES:
        return True
    return len(cell.encode("utf-8", "surrogatepass")) > MAX_CELL_UTF8_BYTES


def _contains_any(index: dict[str, tuple[str, ...]], window: int, text: str) -> bool:
    if not index or len(text) < window:
        return False
    lookup = index.get
    for start in range(len(text) - window + 1):
        candidates = lookup(text[start : start + window])
        if candidates is not None:
            for pattern in candidates:
                if text.startswith(pattern, start):
                    return True
    return False


def classify_cell(patterns: CanaryPatterns, cell: str) -> CellOutcome:
    """The strongest mode that matches ``cell``, or why it is not a finding."""
    if _oversized(cell):
        return CellOutcome.OVERSIZED
    if cell in patterns.exact:
        return CellOutcome.EXACT_CELL
    if _contains_any(patterns.span_index, patterns.window, cell):
        return CellOutcome.EXACT_SPAN
    norm = normalize_m3(cell)
    if norm is None:
        return CellOutcome.UNCOMPARED
    if _contains_any(patterns.normalized_index, patterns.window, norm):
        return CellOutcome.NFC_WHITESPACE_SPAN
    return CellOutcome.NO_MATCH


def tally_cells(patterns: CanaryPatterns, cells: Iterable[str]) -> CellTally:
    """One count per cell, under its single ``classify_cell`` outcome."""
    counts = dict.fromkeys(CellOutcome, 0)
    for cell in cells:
        if type(cell) is not str:
            raise CanaryPolicyInputError
        counts[classify_cell(patterns, cell)] += 1
    return CellTally(**{o.value: counts[o] for o in CellOutcome})
