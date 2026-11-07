"""Microaggregation of numeric quasi-identifiers (MDAV).

``microaggregate(table, quasi_identifiers, k=k)`` partitions the rows into
groups of k to 2k-1 records that are close on the named numeric columns and
replaces each record's values by its group's mean, so the result is
k-anonymous on those columns. The partition is MDAV (Domingo-Ferrer and Torra
2005) as sdcMicro's ``microaggregation(method = "mdav")`` runs it, on columns
centred and scaled by their population standard deviation.

Contract:

- Group sizes: every group has k to 2k-1 rows; every group but the last one
  formed has exactly k.
- Every member of a group gets the same value, bit for bit, in every named
  column; a group's value is the float64 mean of its members' values, so
  column totals are kept up to rounding. Integer columns come back float64;
  rounding them afterwards keeps the groups intact.
- A column whose values are all equal is passed through as-is (float64) and
  does not affect the grouping.
- Deterministic: no randomness; ties go to the earliest row. The same table in
  the same row order gives the same bits on the same platform and library
  versions. With no exact distance ties, row order does not change the groups.
- Whole-table: groups depend on every row, so running per chunk gives
  different (still k-anonymous per chunk) groups.
- Input is a ``pandas.DataFrame`` or ``pyarrow.Table`` and is never modified;
  the result's table is a new object of the same type, other columns unchanged
  (Arrow field metadata of the replaced columns is not kept).

Refusals (``TypeError`` / ``ValueError``, value-free, no exception chain):
``k`` not exactly an ``int`` or below 2; ``quasi_identifiers`` not a non-empty
list or tuple of distinct ``str``; a column missing or present twice; a column
that is not int, uint or float (bool, decimal, string, category, datetime and
object columns are refused even if they hold numbers); nulls or NaN; inf; a
column too large to standardise; more than ``MAX_ROWS`` rows; 1 to k-1 rows.
An empty table is accepted.

Cost: O(n^2 * p / k) time, about ``2 * 8 * n * p`` bytes. Measured at
k = 2, p = 4: 10,000 rows in about 2 s, 50,000 rows in about 50 s; hence
``MAX_ROWS = 50_000``. Larger tables need a faster method (F-MDAV) or strata.

The report (``MicroaggregationReport``) gives group sizes and SSE/SST
information loss on the standardised data, overall and per column; it carries
no cell value.

What this does not prove: k-anonymity on the named columns only. Published
means of tight groups reveal their members' values closely; knowing k-1
members' values gives the last; sensitive columns are not diversified (see
``docs/what-we-cannot-prove.md``).
"""

from __future__ import annotations

from decoy_engine.anonymize.microaggregation._api import (
    MicroaggregationResult,
    microaggregate,
)
from decoy_engine.anonymize.microaggregation._inputs import MAX_ROWS
from decoy_engine.anonymize.microaggregation._report import MicroaggregationReport

__all__ = [
    "MAX_ROWS",
    "MicroaggregationReport",
    "MicroaggregationResult",
    "microaggregate",
]
