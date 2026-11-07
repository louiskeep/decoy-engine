"""Syntactic privacy models measured on a table: k-anonymity, l-diversity, t-closeness.

Measurement only. Nothing here generalizes, suppresses or fails a run, and
no config, planner check or validator reaches it; callers import it.

Models (each module cites its source):

- ``measure_k_anonymity``: equivalence-class sizes over the named
  quasi-identifiers (Sweeney 2002), with ARX's prosecutor risk figures.
- ``measure_l_diversity``: distinct, entropy and recursive (c, l)
  l-diversity of one sensitive column (Machanavajjhala et al. 2007).
- ``measure_t_closeness_equal`` / ``measure_t_closeness_ordered``: the
  largest Earth Mover's Distance between a class's sensitive distribution
  and the whole table's, with equal or ordered ground distance (Li, Li and
  Venkatasubramanian 2007).

Input is a ``pandas.DataFrame`` or a ``pyarrow.Table``; only the named
columns are read, and Arrow columns convert losslessly.

Null policy:

- A null quasi-identifier is its own value: null rows share a class only
  with rows null in the same columns and equal elsewhere. Unlike sdcMicro's
  wildcard treatment this never merges a null row into a bigger class.
  ``None``, ``NaN`` (including a NaN inside an Arrow float column),
  ``pd.NA``, ``NaT`` and Arrow nulls are all the same null.
- Null sensitive values are left out of every distribution. For l-diversity
  that is conservative (a null never looks like diversity); a class with no
  non-null sensitive value has l 0 and fails any l target.
- For t-closeness such a class cannot be measured, so a target is reported
  as undetermined (``meets_target is None``) unless a measured class already
  exceeds it. When null itself carries meaning (for example "not tested"),
  a difference in null rates between classes is a disclosure this measure
  does not see; replace such nulls with an explicit value before measuring.

Value identity: quasi-identifier cells are equal when they are equal and of
the same type family (Python and numpy ints are one family, as are floats,
strings, and datetimes with ``pd.Timestamp``; ``bool`` is its own family),
so ``1``, ``1.0`` and ``True`` are three values. Sensitive cells are keyed the
same way for t-closeness (merging could only lower the distance) and by
plain equality for l-diversity (merging can only lower l). Timezone-aware
timestamps for the same instant in different zones are one value.

Reports are frozen and value-free: counts, measures, booleans and fixed
tokens, never a cell value. They describe these syntactic models only and
prove nothing about linkage with outside data (see
``docs/what-we-cannot-prove.md``).
"""

from __future__ import annotations

from decoy_engine.quality.anonymity._k_anonymity import KAnonymityReport, measure_k_anonymity
from decoy_engine.quality.anonymity._l_diversity import LDiversityReport, measure_l_diversity
from decoy_engine.quality.anonymity._t_closeness import (
    TClosenessReport,
    measure_t_closeness_equal,
    measure_t_closeness_ordered,
)

__all__ = [
    "KAnonymityReport",
    "LDiversityReport",
    "TClosenessReport",
    "measure_k_anonymity",
    "measure_l_diversity",
    "measure_t_closeness_equal",
    "measure_t_closeness_ordered",
]
