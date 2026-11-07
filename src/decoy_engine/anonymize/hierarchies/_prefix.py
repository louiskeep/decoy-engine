"""Prefix (redaction) hierarchy for fixed-length codes such as ZIP codes.

Pattern: ARX redaction-based hierarchy (Prasser and Kohlmayer, ARX Data Anonymization Tool, Apache-2.0).
  Level j keeps a shorter prefix and masks the rest with ``*`` from the right,
  so ``"02134"`` becomes ``"0213*"``, ``"021**"`` and so on. ARX pads values to
  the longest value seen; here the length is declared and other lengths are
  not covered, so a label never depends on other rows. See:
  https://github.com/arx-deidentifier/arx (HierarchyBuilderRedactionBased).

Labels come from the engine's ``truncate`` kernel (``kernel.truncate_array``
with ``keep="head"`` and mask character ``*``). Covered values: ``str`` of
exactly ``length`` characters. Integers are not covered: a ZIP code stored as
a number has lost its leading zeros.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

import pyarrow as pa

from decoy_engine.anonymize._values import type_family
from decoy_engine.anonymize.hierarchies._base import SUPPRESSED, Hierarchy
from decoy_engine.kernel import truncate_array


@dataclass(frozen=True, slots=True)
class PrefixHierarchy(Hierarchy):
    """Keep the first ``keep[i]`` characters at level ``i + 1``; mask the rest."""

    length: int
    keep: tuple[int, ...]
    kind: ClassVar[str] = "prefix"

    def __post_init__(self) -> None:
        length, keep = _checked(self.length, self.keep)
        object.__setattr__(self, "length", length)
        object.__setattr__(self, "keep", keep)

    @property
    def height(self) -> int:
        return len(self.keep) + 1

    def _covers(self, value: Any) -> bool:
        return type_family(value) == "str" and len(value) == self.length

    def _labels(self, values: Sequence[Any], level: int) -> list[str]:
        masked = truncate_array(
            pa.array([str(v) for v in values], type=pa.string()),
            length=self.keep[level - 1],
            keep="head",
            mask_char=SUPPRESSED,
        )
        return [str(label) for label in masked.to_pylist()]


def prefix_hierarchy(length: int, *, keep: Sequence[int] | None = None) -> PrefixHierarchy:
    """Prefix levels over codes of exactly ``length`` characters.

    ``keep`` lists the prefix lengths kept at levels 1, 2, ..., strictly
    decreasing within ``1..length-1``; the default keeps one character fewer
    at each level.
    """
    if keep is None and type(length) is int:
        keep = tuple(range(length - 1, 0, -1))
    return PrefixHierarchy(length=length, keep=keep)  # type: ignore[arg-type]


def _checked(length: object, keep: object) -> tuple[int, tuple[int, ...]]:
    if type(length) is not int:
        raise TypeError("length must be an int (not bool, float or a numpy scalar)")
    if length < 2:
        raise ValueError("length must be at least 2")
    if isinstance(keep, (str, bytes)) or not isinstance(keep, Sequence):
        raise TypeError("keep must be a sequence of ints")
    kept = tuple(keep)
    if not kept:
        raise ValueError("keep must name at least one level")
    for size in kept:
        if type(size) is not int:
            raise TypeError("keep must hold ints (not bool, float or a numpy scalar)")
        if not 1 <= size <= length - 1:
            raise ValueError(f"keep values must be between 1 and {length - 1}")
    if any(b >= a for a, b in itertools.pairwise(kept)):
        raise ValueError("keep must be strictly decreasing")
    return length, kept
