"""Privacy models as descriptors: which equivalence classes fail a target.

Pattern: ARX privacy criteria (Prasser and Kohlmayer, ARX Data Anonymization Tool, Apache-2.0).
  One object per model, as ARX's ``config.addPrivacyModel(new KAnonymity(5))``,
  each declaring ARX's monotonicity flag under suppression (KAnonymity and
  DistinctLDiversity monotone; EntropyLDiversity, RecursiveCLDiversity and
  TCloseness not). The per-class rules are the V2-3.2 definitions: Sweeney
  2002 (k), Machanavajjhala et al. 2007 (l), Li, Li and Venkatasubramanian
  2007 (t). See: https://github.com/arx-deidentifier/arx (criteria package).

Pre-GA and unstable: the descriptor shape may change before config wiring.

A class with no non-null sensitive value fails every l and t model: an
unmeasured class must not let a release pass. t compares each class with the
distribution of every row the frequency set was built from, which stays fixed
under rollup (ARX takes it once from the input data).

Descriptors hold a column name and thresholds only, never a cell value.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import numpy as np
import numpy.typing as npt
import pandas as pd

from decoy_engine.quality.anonymity._classes import (
    IntArray,
    SensitiveCounts,
    check_int_target,
    check_positive_real,
    check_unit_interval,
    ordered_sensitive_codes,
    sensitive_codes,
    sensitive_family_codes,
)
from decoy_engine.quality.anonymity._l_diversity import (
    _entropies,
    _recursive_levels,
    entropy_below,
)
from decoy_engine.quality.anonymity._t_closeness import _equal_emd, _ordered_emd, emd_above

if TYPE_CHECKING:
    from decoy_engine.quality.anonymity._frequency_set import FrequencySet

BoolArray = npt.NDArray[np.bool_]
Coder = Callable[[pd.Series, str], IntArray]


def _required_int(name: str, value: object) -> int:
    checked = check_int_target(name, value)
    if checked is None:
        raise TypeError(f"{name} is required")
    return checked


def _required_real(name: str, value: object) -> float:
    checked = check_positive_real(name, value)
    if checked is None:
        raise TypeError(f"{name} is required")
    return checked


def _required_unit(name: str, value: object) -> float:
    checked = check_unit_interval(name, value)
    if checked is None:
        raise TypeError(f"{name} is required")
    return checked


def _required_column(value: object) -> str:
    if type(value) is not str:
        raise TypeError("sensitive must be a column name (str)")
    return value


class PrivacyModel(abc.ABC):
    """A privacy target that some equivalence classes may fail."""

    __slots__ = ()

    # ARX's monotonicWithSuppression: when true, the rows failing at a node
    # include the rows failing at any successor.
    monotone_with_suppression: ClassVar[bool]
    # The model's token in written reports; one per descriptor.
    kind: ClassVar[str]

    @property
    def sensitive_column(self) -> str | None:
        """The sensitive column this model reads, or None."""
        return None

    @property
    def coder(self) -> Coder | None:
        """How sensitive cells are coded (equality, type family or rank)."""
        return None

    @property
    def reads_order(self) -> bool:
        """Whether the sensitive column's category order is read."""
        return False

    @abc.abstractmethod
    def failing_classes(self, frequencies: FrequencySet) -> BoolArray:
        """One flag per class of ``frequencies``: True where the class fails."""


@dataclass(frozen=True, slots=True)
class KAnonymity(PrivacyModel):
    """Every class has at least ``k`` rows (Sweeney 2002)."""

    k: int
    monotone_with_suppression: ClassVar[bool] = True
    kind: ClassVar[str] = "k_anonymity"

    def __post_init__(self) -> None:
        _required_int("k", self.k)

    def failing_classes(self, frequencies: FrequencySet) -> BoolArray:
        return frequencies.sizes < self.k


@dataclass(frozen=True, slots=True)
class _SensitiveModel(PrivacyModel):
    sensitive: str

    def __post_init__(self) -> None:
        _required_column(self.sensitive)

    @property
    def sensitive_column(self) -> str | None:
        return self.sensitive

    def _counts(self, frequencies: FrequencySet) -> SensitiveCounts:
        return frequencies.sensitive(self)


@dataclass(frozen=True, slots=True)
class DistinctLDiversity(_SensitiveModel):
    """At least ``l`` distinct non-null sensitive values per class."""

    l: int  # noqa: E741 -- the model's published name
    monotone_with_suppression: ClassVar[bool] = True
    kind: ClassVar[str] = "distinct_l_diversity"

    def __post_init__(self) -> None:
        _required_column(self.sensitive)
        _required_int("l", self.l)

    @property
    def coder(self) -> Coder | None:
        return sensitive_codes

    def failing_classes(self, frequencies: FrequencySet) -> BoolArray:
        counts = self._counts(frequencies)
        distinct = np.bincount(counts.pair_class, minlength=frequencies.count)
        return distinct < self.l


@dataclass(frozen=True, slots=True)
class EntropyLDiversity(_SensitiveModel):
    """Sensitive entropy at least ``ln l`` per class."""

    l: int  # noqa: E741 -- the model's published name
    monotone_with_suppression: ClassVar[bool] = False
    kind: ClassVar[str] = "entropy_l_diversity"

    def __post_init__(self) -> None:
        _required_column(self.sensitive)
        _required_int("l", self.l)

    @property
    def coder(self) -> Coder | None:
        return sensitive_codes

    def failing_classes(self, frequencies: FrequencySet) -> BoolArray:
        counts = self._counts(frequencies)
        entropy = _entropies(counts, frequencies.count)
        return entropy_below(entropy, counts.class_totals == 0, self.l)


@dataclass(frozen=True, slots=True)
class RecursiveCLDiversity(_SensitiveModel):
    """Recursive (c, l)-diversity: ``r_1 < c (r_l + ... + r_d)`` per class."""

    c: float
    l: int  # noqa: E741 -- the model's published name
    monotone_with_suppression: ClassVar[bool] = False
    kind: ClassVar[str] = "recursive_cl_diversity"

    def __post_init__(self) -> None:
        _required_column(self.sensitive)
        _required_real("c", self.c)
        _required_int("l", self.l)

    @property
    def coder(self) -> Coder | None:
        return sensitive_codes

    def failing_classes(self, frequencies: FrequencySet) -> BoolArray:
        counts = self._counts(frequencies)
        levels = _recursive_levels(counts, frequencies.count, float(self.c))
        return levels < self.l


@dataclass(frozen=True, slots=True)
class _TModel(_SensitiveModel):
    t: float

    def __post_init__(self) -> None:
        _required_column(self.sensitive)
        _required_unit("t", self.t)

    def _emd(self, counts: SensitiveCounts, measured: IntArray) -> npt.NDArray[np.float64]:
        raise NotImplementedError

    def failing_classes(self, frequencies: FrequencySet) -> BoolArray:
        counts = self._counts(frequencies)
        unmeasured = counts.class_totals == 0
        measured = np.flatnonzero(~unmeasured).astype(np.int64)
        over = unmeasured.copy()
        if measured.size:
            over[measured] = emd_above(self._emd(counts, measured), float(self.t))
        return over


@dataclass(frozen=True, slots=True)
class EqualTCloseness(_TModel):
    """Equal-distance EMD to the table distribution at most ``t``."""

    monotone_with_suppression: ClassVar[bool] = False
    kind: ClassVar[str] = "equal_t_closeness"

    @property
    def coder(self) -> Coder | None:
        return sensitive_family_codes

    def _emd(self, counts: SensitiveCounts, measured: IntArray) -> npt.NDArray[np.float64]:
        return _equal_emd(counts, measured)


@dataclass(frozen=True, slots=True)
class OrderedTCloseness(_TModel):
    """Ordered-distance EMD to the table distribution at most ``t``."""

    monotone_with_suppression: ClassVar[bool] = False
    kind: ClassVar[str] = "ordered_t_closeness"

    @property
    def coder(self) -> Coder | None:
        return ordered_sensitive_codes

    @property
    def reads_order(self) -> bool:
        return True

    def _emd(self, counts: SensitiveCounts, measured: IntArray) -> npt.NDArray[np.float64]:
        return _ordered_emd(counts, measured)
