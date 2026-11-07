"""Generalization hierarchies for quasi-identifiers, and their lattice.

Pattern: domain and value generalization hierarchies (Sweeney 2002; Samarati 2001) built the ARX way.
  See: L. Sweeney, "Achieving k-anonymity privacy protection using
  generalization and suppression", IJUFKS 10(5), 2002; P. Samarati,
  "Protecting respondents' identities in microdata release", IEEE TKDE 13(6),
  2001; ARX Data Anonymization Tool (hierarchy builders).

A hierarchy maps a column to ordered levels ``0..height``:

- level 0 is the column unchanged;
- levels ``1..height-1`` are string labels; a null stays null;
- level ``height`` is ``"*"`` (``SUPPRESSED``) for every row, nulls included.

Coarser levels only merge classes of finer ones (monotone), and a label
depends only on the value, never on other rows, row order or chunking.
Cells are the same value only when equal and of the same type family
(``decoy_engine.anonymize.type_family``): ``1``, ``1.0`` and ``True`` are three
values. An object column of datetimes must hold one time zone.

Builders:

- ``interval_hierarchy(widths)``: fixed-width ``[lo, hi)`` intervals
  (``bucketize``'s binning), each width a multiple of the previous one.
- ``band_hierarchy(cuts)``: bands between cut points, open at both ends;
  each coarser level's cuts a proper subset of the finer one's.
- ``prefix_hierarchy(length, keep=None)``: fixed-length codes such as ZIPs,
  masked from the right (``"021**"``).
- ``date_hierarchy(levels)``: month (``"2024-03"``), quarter (``"2024Q1"``),
  year (``"2024"``).
- ``category_hierarchy(rows)``: a user tree as ARX-style rows
  ``(value, level-1 label, ...)``.

Each kind is a frozen dataclass exposing ``kind``, ``height`` and its defining
fields (``widths``, ``cuts``, ``length`` and ``keep``, ``levels``, ``rows``).

``Hierarchy.generalize(column, level)`` takes a ``pandas.Series`` (returns a
``Series``) or a ``pyarrow.Array``/``ChunkedArray`` (returns a ``pa.Array``)
and refuses a non-null value the hierarchy does not cover.
``check_hierarchy(hierarchy, column)`` reports coverage, classes per level and
monotonicity without changing anything. ``Lattice(mapping)`` is the lattice of
level vectors over named columns; ``Lattice.apply(table, node)`` returns a new
table.

Errors and reports never carry a cell value. Generalization alone proves
nothing about linkage with outside data; measure the result (k-anonymity,
l-diversity, t-closeness) before calling it anonymous.
"""

from __future__ import annotations

from decoy_engine.anonymize.hierarchies._base import SUPPRESSED, Hierarchy
from decoy_engine.anonymize.hierarchies._category import CategoryHierarchy, category_hierarchy
from decoy_engine.anonymize.hierarchies._check import HierarchyCheck, check_hierarchy
from decoy_engine.anonymize.hierarchies._dates import DateHierarchy, date_hierarchy
from decoy_engine.anonymize.hierarchies._lattice import Lattice
from decoy_engine.anonymize.hierarchies._numeric import (
    BandHierarchy,
    IntervalHierarchy,
    band_hierarchy,
    interval_hierarchy,
)
from decoy_engine.anonymize.hierarchies._prefix import PrefixHierarchy, prefix_hierarchy

__all__ = [
    "SUPPRESSED",
    "BandHierarchy",
    "CategoryHierarchy",
    "DateHierarchy",
    "Hierarchy",
    "HierarchyCheck",
    "IntervalHierarchy",
    "Lattice",
    "PrefixHierarchy",
    "band_hierarchy",
    "category_hierarchy",
    "check_hierarchy",
    "date_hierarchy",
    "interval_hierarchy",
    "prefix_hierarchy",
]
