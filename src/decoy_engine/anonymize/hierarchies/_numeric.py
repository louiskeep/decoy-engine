"""Numeric hierarchies: fixed-width intervals and cut-point bands.

Pattern: ARX interval-based hierarchy (Prasser and Kohlmayer, ARX Data Anonymization Tool, Apache-2.0).
  Half-open intervals ``[lo, hi)``; each coarser level is a union of whole
  intervals of the finer one (ARX's grouping by fanout), which makes the
  hierarchy monotone by construction. Values below or above the outer cut
  points of a band level are bottom- and top-coded into open bands, as ARX's
  bottom/top coding does. Recoding is global, the same rule for every row
  (sdcMicro ``globalRecode``). See:
  https://github.com/arx-deidentifier/arx (HierarchyBuilderIntervalBased).

``interval_hierarchy`` keeps the ``bucketize`` binning rule
``floor(value / width) * width`` (origin 0) and requires each width to be a
multiple of at least 2 of the one before, so every coarse interval is a union
of finer ones. ``band_hierarchy`` takes cut points per level; each coarser
level's cuts must be a proper subset of the finer level's.

Bounds are exact. Integers use Python int arithmetic (no int64 overflow).
Floats use their exact rational value (``as_integer_ratio``), because float
division can round across a bound for values above 2**53 and for subnormals,
which would put a value outside its own label and split classes across
levels.

Covered values: ints and finite floats (Python or numpy). A float wider than
64 bits (``numpy.longdouble``) is covered only when a double holds it exactly.
``bool``, ``Decimal``, text and infinities are not covered.
"""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, cast

import numpy as np

from decoy_engine.anonymize._values import type_family
from decoy_engine.anonymize.hierarchies._base import Hierarchy

Number = int | float


def _as_number(value: Any) -> Number | None:
    """The value as an exact Python int or finite float, or None if not covered."""
    family = type_family(value)
    if family == "int":
        return int(value)
    if family == "float":
        number = float(value)
        if isinstance(value, np.floating) and value.dtype.itemsize > 8 and number != value:
            # A wider float that a double cannot hold exactly would be rounded;
            # equal values share this outcome, so row order cannot change it.
            return None
        return number if math.isfinite(number) else None
    return None


def floor_multiple(value: Number, width: int) -> int:
    """The largest multiple of ``width`` that is ``<= value``, exactly."""
    if isinstance(value, int):
        return (value // width) * width
    numerator, denominator = value.as_integer_ratio()
    return (numerator // (denominator * width)) * width


def _format(number: Number) -> str:
    if isinstance(number, int):
        return str(number)
    if number.is_integer():
        return str(int(number))
    return repr(number)


@dataclass(frozen=True, slots=True)
class IntervalHierarchy(Hierarchy):
    """Fixed-width intervals, one width per level, finest first."""

    widths: tuple[int, ...]
    kind: ClassVar[str] = "interval"

    def __post_init__(self) -> None:
        object.__setattr__(self, "widths", _checked_widths(self.widths))

    @property
    def height(self) -> int:
        return len(self.widths) + 1

    def _covers(self, value: Any) -> bool:
        return _as_number(value) is not None

    def _labels(self, values: Sequence[Any], level: int) -> list[str]:
        width = self.widths[level - 1]
        out = []
        for value in values:
            number = cast(Number, _as_number(value))  # covered values only
            low = floor_multiple(number, width)
            out.append(f"[{low}, {low + width})")
        return out


@dataclass(frozen=True, slots=True)
class BandHierarchy(Hierarchy):
    """Bands between cut points, one strictly increasing cut tuple per level, finest first."""

    cuts: tuple[tuple[Number, ...], ...]
    kind: ClassVar[str] = "band"

    def __post_init__(self) -> None:
        object.__setattr__(self, "cuts", _checked_cuts(self.cuts))

    @property
    def height(self) -> int:
        return len(self.cuts) + 1

    def _covers(self, value: Any) -> bool:
        return _as_number(value) is not None

    def _labels(self, values: Sequence[Any], level: int) -> list[str]:
        cuts = self.cuts[level - 1]
        out = []
        for value in values:
            number = cast(Number, _as_number(value))  # covered values only
            # Python compares int and float exactly, so bisect places 2**53 + 1 right.
            index = bisect.bisect_right(cuts, number)
            low = "-inf" if index == 0 else _format(cuts[index - 1])
            high = "inf" if index == len(cuts) else _format(cuts[index])
            out.append(f"[{low}, {high})")
        return out


def interval_hierarchy(widths: Sequence[int]) -> IntervalHierarchy:
    """Fixed-width intervals; each width a multiple (at least 2) of the previous one."""
    return IntervalHierarchy(widths=widths)  # type: ignore[arg-type]


def band_hierarchy(cuts: Sequence[Sequence[Number]]) -> BandHierarchy:
    """Bands between cut points; each coarser level's cuts a proper subset of the finer one's."""
    return BandHierarchy(cuts=cuts)  # type: ignore[arg-type]


def _checked_widths(widths: object) -> tuple[int, ...]:
    if isinstance(widths, (str, bytes)) or not isinstance(widths, Sequence):
        raise TypeError("widths must be a sequence of ints")
    checked = tuple(widths)
    if not checked:
        raise ValueError("widths must name at least one level")
    for width in checked:
        if type(width) is not int:
            raise TypeError("widths must be ints (not bool, float or a numpy scalar)")
        if width < 1:
            raise ValueError("widths must be at least 1")
    for finer, coarser in itertools.pairwise(checked):
        if coarser % finer or coarser // finer < 2:
            raise ValueError("each width must be a multiple (at least 2) of the previous width")
    return checked


def _checked_cuts(cuts: object) -> tuple[tuple[Number, ...], ...]:
    if isinstance(cuts, (str, bytes)) or not isinstance(cuts, Sequence):
        raise TypeError("cuts must be a sequence of cut-point sequences")
    levels: list[tuple[Number, ...]] = []
    for level in cuts:
        if isinstance(level, (str, bytes)) or not isinstance(level, Sequence):
            raise TypeError("each level of cuts must be a sequence of numbers")
        points = tuple(level)
        if not points:
            raise ValueError("each level of cuts must have at least one cut point")
        for point in points:
            if type(point) not in (int, float):
                raise TypeError("cut points must be int or float (not bool or a numpy scalar)")
            if not math.isfinite(point):
                raise ValueError("cut points must be finite")
        if any(b <= a for a, b in itertools.pairwise(points)):
            raise ValueError("cut points in a level must be strictly increasing")
        levels.append(points)
    if not levels:
        raise ValueError("cuts must name at least one level")
    for finer, coarser in itertools.pairwise(levels):
        if not set(coarser) < set(finer):
            raise ValueError(
                "each level's cut points must be a proper subset of the previous level's"
            )
    return tuple(levels)
