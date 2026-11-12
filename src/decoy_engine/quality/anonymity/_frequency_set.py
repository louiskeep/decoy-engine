"""Frequency sets: equivalence classes with their sizes and sensitive counts, and their rollup.

Pattern: Incognito frequency set and rollup (LeFevre, DeWitt and Ramakrishnan 2005).
  A frequency set maps each distinct quasi-identifier combination to its
  count; the frequency set of a coarser generalization is the sum of the
  finer one's counts over the classes it merges, so a lattice search
  never regroups the rows. See: K. LeFevre, D. J. DeWitt and R.
  Ramakrishnan, "Incognito: efficient full-domain k-anonymity", SIGMOD 2005.

Pre-GA and unstable: the shape may change before config wiring.

``FrequencySet.of`` builds the base classes of a table through the same
``equivalence_classes`` the measures use (one class identity, one null
policy), keeps per base class its size and first row position, and per
sensitive coding the sparse (class, value) counts. ``rollup`` merges classes
by integer codes, again through ``equivalence_classes``. The table
distribution of each sensitive coding is kept from the base rows, so a
t-closeness check after rollup compares with the whole input, as ARX does.

Everything held is an integer array: no cell value.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa

from decoy_engine.quality.anonymity._classes import (
    IntArray,
    SensitiveCounts,
    check_columns,
    equivalence_classes,
    read_columns,
    sensitive_counts,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from decoy_engine.quality.anonymity._models import Coder, PrivacyModel

_CountsKey = tuple[str, Any]


@dataclass(frozen=True, eq=False)
class FrequencySet:
    """Classes over a table's rows, as integer arrays.

    ``sizes[c]`` is the row count of class ``c``; ``base_class[b]`` maps base
    class ``b`` (a class of ``of``) to its class here; ``row_base[r]`` maps
    input row ``r`` to its base class; ``representatives[b]`` is the first row
    position of base class ``b``.
    """

    sizes: IntArray
    base_class: IntArray
    row_base: IntArray
    representatives: IntArray
    counts: Mapping[_CountsKey, SensitiveCounts]

    @property
    def count(self) -> int:
        return int(self.sizes.shape[0])

    @property
    def rows(self) -> int:
        return int(self.row_base.shape[0])

    @property
    def row_class(self) -> IntArray:
        """Class of each input row."""
        return self.base_class[self.row_base]

    @classmethod
    def of(
        cls,
        table: pd.DataFrame | pa.Table,
        quasi_identifiers: Sequence[str],
        models: Sequence[PrivacyModel] = (),
    ) -> FrequencySet:
        """Base classes of ``table`` over ``quasi_identifiers``, with the
        sensitive counts ``models`` need. Validates names as the measures do."""
        qi = check_columns(table, quasi_identifiers, None)
        sensitive: list[str] = []
        ordered: set[str] = set()
        keys: list[tuple[str, Coder]] = []
        for model in models:
            column = model.sensitive_column
            coder = model.coder
            if column is None or coder is None:
                continue
            check_columns(table, qi, column)
            if column not in sensitive:
                sensitive.append(column)
            if model.reads_order:
                ordered.add(column)
            if (column, coder) not in keys:
                keys.append((column, coder))
        frame = read_columns(table, (*qi, *sensitive), frozenset(ordered))
        classes = equivalence_classes(frame, qi)
        count = classes.count
        _, first = np.unique(classes.ids, return_index=True)
        counts: dict[_CountsKey, SensitiveCounts] = {}
        for column, coder in keys:
            position = len(qi) + sensitive.index(column)
            codes = coder(frame.iloc[:, position], column)
            counts[(column, coder)] = sensitive_counts(classes, codes)
        return cls(
            sizes=classes.sizes,
            base_class=np.arange(count, dtype=np.int64),
            row_base=classes.ids,
            representatives=first.astype(np.int64),
            counts=MappingProxyType(counts),
        )

    def sensitive(self, model: PrivacyModel) -> SensitiveCounts:
        """The sparse counts for ``model``'s sensitive column and coding."""
        key = (model.sensitive_column, model.coder)
        found = self.counts.get(key)  # type: ignore[arg-type]
        if found is None:
            raise ValueError("this frequency set was not built for that model's sensitive column")
        return found

    def rollup(self, column_codes: Sequence[IntArray]) -> FrequencySet:
        """Merge classes that share every code (one int array per column, one
        entry per class here); sizes and sensitive counts are summed."""
        if not column_codes:
            raise ValueError("rollup needs at least one code column")
        for codes in column_codes:
            if codes.shape != (self.count,):
                raise ValueError("each code column needs one entry per class")
        frame = _combined(column_codes)
        merged = equivalence_classes(frame, tuple(frame.columns))
        mapping = merged.ids
        new_count = merged.count
        sizes = np.zeros(new_count, dtype=np.int64)
        np.add.at(sizes, mapping, self.sizes)
        counts = {key: _rolled(value, mapping, new_count) for key, value in self.counts.items()}
        return FrequencySet(
            sizes=sizes,
            base_class=mapping[self.base_class],
            row_base=self.row_base,
            representatives=self.representatives,
            counts=MappingProxyType(counts),
        )

    def failing(self, models: Sequence[PrivacyModel]) -> npt.NDArray[np.bool_]:
        """Classes failing at least one model."""
        out = np.zeros(self.count, dtype=bool)
        for model in models:
            out |= model.failing_classes(self)
        return out

    def row_mask(self, class_mask: npt.NDArray[np.bool_]) -> npt.NDArray[np.bool_]:
        """Input rows whose class is flagged in ``class_mask``."""
        return np.asarray(class_mask, dtype=bool)[self.row_class]


def _combined(column_codes: Sequence[IntArray]) -> pd.DataFrame:
    """One integer key per class when the code ranges fit int64 (one column to
    group instead of several), else the code columns as they are."""
    columns = [np.asarray(c, dtype=np.int64) for c in column_codes]
    radix = [int(c.max()) + 1 if c.size else 1 for c in columns]
    if math.prod(radix) < 2**62 and all(c.size == 0 or int(c.min()) >= 0 for c in columns):
        key: IntArray = np.zeros(columns[0].shape[0], dtype=np.int64)
        for codes, base in zip(columns, radix, strict=True):
            key = np.asarray(key * base + codes, dtype=np.int64)
        return pd.DataFrame({"codes": key})
    return pd.DataFrame({str(i): c for i, c in enumerate(columns)})


def _rolled(counts: SensitiveCounts, mapping: IntArray, new_count: int) -> SensitiveCounts:
    """Sparse counts summed over merged classes; the table totals are unchanged."""
    divisor = max(counts.value_count, 1)
    keys = mapping[counts.pair_class] * divisor + counts.pair_value
    unique_keys, inverse = np.unique(keys, return_inverse=True)
    pair_count = np.zeros(unique_keys.shape[0], dtype=np.int64)
    np.add.at(pair_count, inverse, counts.pair_count)
    class_totals = np.zeros(new_count, dtype=np.int64)
    np.add.at(class_totals, mapping, counts.class_totals)
    return SensitiveCounts(
        pair_class=(unique_keys // divisor).astype(np.int64),
        pair_value=(unique_keys % divisor).astype(np.int64),
        pair_count=pair_count,
        class_totals=class_totals,
        value_totals=counts.value_totals,
        value_count=counts.value_count,
        rows_with_null=counts.rows_with_null,
    )
