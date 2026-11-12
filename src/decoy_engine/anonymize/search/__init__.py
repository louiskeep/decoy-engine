"""Optimal full-domain generalization with record suppression.

Pattern: least-loss lattice search under privacy models with a suppression limit (ARX; Incognito; Flash).
  See the ``_search`` module docstring for the method and why the result is
  optimal; ``_loss`` for the three metrics.

``search_lattice(table, lattice, models, *, suppression_limit=0.0,
metric=LossMetric())`` returns a frozen, value-free ``SearchResult``: the
node of ``lattice`` with the least information loss whose failing classes
(classes failing any model from ``decoy_engine.quality.anonymity``) hold at
most ``floor(suppression_limit * rows)`` rows, the limit read as the decimal
it prints as (``0.29`` of 100 rows allows 29). Ties go to fewer suppressed
rows, then fewer levels, then the lexicographically smaller node. When no
node qualifies the result says so (``found=False``, ``node=None``).

``apply_search(table, lattice, result)`` returns ``Released``: ``table`` is
the input generalized to the node with the suppressed rows removed,
``suppressed`` flags those rows by input position, and
``generalized_including_suppressed`` keeps every row.

What the guarantee covers, and what it does not:

- Every class left after suppression meets every model. t-closeness is
  measured against the sensitive distribution of all input rows (ARX's
  rule), so on ``Released.table`` the V2-3.2 t measures, which use the
  remaining rows, can report a larger distance.
- The suppressed rows are not checked against the models (ARX's
  semantics). ``generalized_including_suppressed`` publishes them generalized
  only to the chosen node.
- Lattices above ``MAX_LATTICE_NODES`` (5,000) nodes are refused, not
  searched heuristically. Levels must be nested on the data; a hierarchy
  whose level splits a class of the level below is refused.
- The models are syntactic: meeting them proves nothing about linkage with
  outside data (see ``docs/what-we-cannot-prove.md``).

Results and errors never carry a cell value. Nothing in config, the planner
or a run reaches this package; callers import it. The package depends on
``decoy_engine.quality.anonymity``, which itself imports only the
``decoy_engine.anonymize`` root (for ``type_family``): a package-level back
edge with no module cycle.
"""

from __future__ import annotations

from decoy_engine.anonymize.search._apply import apply_search
from decoy_engine.anonymize.search._loss import (
    Discernibility,
    LossMetric,
    Metric,
    NonUniformEntropy,
)
from decoy_engine.anonymize.search._result import Released, SearchResult
from decoy_engine.anonymize.search._search import MAX_LATTICE_NODES, search_lattice

__all__ = [
    "MAX_LATTICE_NODES",
    "Discernibility",
    "LossMetric",
    "Metric",
    "NonUniformEntropy",
    "Released",
    "SearchResult",
    "apply_search",
    "search_lattice",
]
